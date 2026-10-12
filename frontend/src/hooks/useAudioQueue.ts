/**
 * useAudioQueue — Web Audio API sequential playback với pre-decode.
 *
 * Tối ưu latency:
 *   - Decode base64 → AudioBuffer NGAY KHI chunk tới (không chờ lúc play)
 *   - Khi đến lượt play, AudioBuffer đã sẵn sàng → zero decode wait
 *
 * Flow per chunk:
 *   1. chunk arrives → enqueueAudio → useEffect kích hoạt → pre-decode bắt đầu
 *   2. previous chunk ends → playNext → lấy AudioBuffer đã decode → play ngay
 */

import { useCallback, useEffect, useRef } from 'react'
import { useChatStore } from '../store/chatStore'
import type { AudioChunkPayload } from '../types'
import { startLipSync, startAmplitudeLipSync, stopLipSync } from './useLipSync'

function base64ToArrayBuffer(b64: string): ArrayBuffer {
  const binary = atob(b64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i)
  }
  return bytes.buffer
}

export function useAudioQueue() {
  const audioCtxRef = useRef<AudioContext | null>(null)
  const isPlayingRef = useRef(false)
  const sourceRef = useRef<AudioBufferSourceNode | null>(null)
  const playbackGeneration = useRef(0)

  // WeakMap: chunk object → Promise<AudioBuffer>
  // Pre-decode kết quả được cache tại đây, tự dọn khi chunk bị GC
  const decodeCache = useRef(new WeakMap<AudioChunkPayload, Promise<AudioBuffer>>())

  const { dequeueAudio, setIsAISpeaking, setCurrentEmotion } =
    useChatStore.getState()

  // Lazy-init + await resume (Chrome autoplay policy)
  const getAudioContext = useCallback(async (): Promise<AudioContext | null> => {
    try {
      if (!audioCtxRef.current) {
        audioCtxRef.current = new AudioContext()
      }
      if (audioCtxRef.current.state === 'suspended') {
        await audioCtxRef.current.resume()
      }
      return audioCtxRef.current
    } catch (err) {
      console.warn('[AudioQueue] AudioContext failed to initialize/resume:', err)
      return null
    }
  }, [])

  // ── PRE-DECODE ──────────────────────────────────────────────────────────────
  // Chạy ngay khi audioQueue có chunk mới, không chờ đến lúc play.
  const audioQueue = useChatStore((s) => s.audioQueue)

  useEffect(() => {
    if (audioQueue.length === 0) return

    // Lấy AudioContext (hoặc tạo mới nếu chưa có)
    const ctx = audioCtxRef.current ?? (() => {
      try {
        const c = new AudioContext()
        audioCtxRef.current = c
        return c
      } catch (err) {
        console.warn('[AudioQueue] AudioContext failed to initialize:', err)
        return null
      }
    })()

    if (!ctx) return

    // Pre-decode tất cả chunk chưa được decode
    for (const chunk of audioQueue) {
      if (!chunk.audio_base64 || decodeCache.current.has(chunk)) continue

      const arrayBuffer = base64ToArrayBuffer(chunk.audio_base64)
      // decodeAudioData trả về Promise — không await, để chạy song song
      decodeCache.current.set(chunk, ctx.decodeAudioData(arrayBuffer))
    }
  }, [audioQueue])

  // ── PLAYBACK ─────────────────────────────────────────────────────────────────
  const playNext = useCallback(async () => {
    if (isPlayingRef.current) return

    const chunk = dequeueAudio()
    if (!chunk) {
      setIsAISpeaking(false)
      return
    }

    // Text-only mode (audio_base64 rỗng — TTS tắt từ client)
    if (!chunk.audio_base64) {
      setCurrentEmotion(chunk.emotion)
      // Không set isAISpeaking=true để avatar không animate lip-sync
      // Drain queue ngay lập tức, không delay thêm
      playNext()
      return
    }

    isPlayingRef.current = true
    const generation = playbackGeneration.current
    setIsAISpeaking(true)
    setCurrentEmotion(chunk.emotion)

    try {
      const ctx = await getAudioContext()
      if (generation !== playbackGeneration.current) return
      if (!ctx) {
        console.warn('[AudioQueue] No AudioContext, skipping audio playback.')
        isPlayingRef.current = false
        playNext()
        return
      }

      // Lấy AudioBuffer từ cache pre-decode (thường đã xong) hoặc decode ngay
      let audioBuffer: AudioBuffer
      const cached = decodeCache.current.get(chunk)
      if (cached) {
        audioBuffer = await cached
        decodeCache.current.delete(chunk)
      } else {
        // Fallback: decode on-the-fly nếu pre-decode chưa kịp chạy
        audioBuffer = await ctx.decodeAudioData(base64ToArrayBuffer(chunk.audio_base64))
      }

      if (generation !== playbackGeneration.current) return
      const source = ctx.createBufferSource()
      source.buffer = audioBuffer
      source.connect(ctx.destination)
      sourceRef.current = source

      source.onended = () => {
        stopLipSync()
        isPlayingRef.current = false
        sourceRef.current = null
        playNext()
      }

      source.start(0)
      // Start lip-sync AFTER source.start() so ctx.currentTime is the correct baseline
      if (chunk.visemes.length > 0) {
        startLipSync(chunk.visemes, ctx)
      } else {
        // Amplitude fallback: no Rhubarb visemes → drive jawOpen from audio RMS
        startAmplitudeLipSync(source)
      }
    } catch (err) {
      if (generation !== playbackGeneration.current) return
      console.error('[AudioQueue] Playback error:', err)
      isPlayingRef.current = false
      playNext()
    }
  }, [dequeueAudio, getAudioContext, setCurrentEmotion, setIsAISpeaking])

  // Kick off playback khi queue có chunk mới
  useEffect(() => {
    if (audioQueue.length > 0 && !isPlayingRef.current) {
      playNext()
    }
  }, [audioQueue.length, playNext])

  const stopPlayback = useCallback(() => {
    playbackGeneration.current++
    if (sourceRef.current) {
      sourceRef.current.onended = null
      try { sourceRef.current.stop() } catch { /* already stopped */ }
      sourceRef.current = null
    }
    stopLipSync()
    isPlayingRef.current = false
    setIsAISpeaking(false)
  }, [setIsAISpeaking])

  useEffect(() => {
    return () => {
      stopPlayback()
      audioCtxRef.current?.close()
    }
  }, [stopPlayback])

  return {
    audioContextRef: audioCtxRef,
    stopPlayback,
    preparePlayback: getAudioContext,
  }
}
