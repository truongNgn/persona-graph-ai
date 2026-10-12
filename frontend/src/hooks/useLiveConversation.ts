import { useCallback, useEffect, useRef, useState } from 'react'
import { API_BASE_URL } from '../config/backend'
import { useChatStore } from '../store/chatStore'

type LiveState = 'off' | 'starting' | 'listening' | 'recording' | 'transcribing' | 'waiting' | 'muted'

/** Hands-free, turn-based speech using the existing STT and streamed TTS paths. */
export function useLiveConversation(onTranscript: (text: string) => boolean, microphoneId: string) {
  const [state, setState] = useState<LiveState>('off')
  const [error, setError] = useState<string | null>(null)
  const [level, setLevel] = useState(0)
  const active = useRef(false)
  const muted = useRef(false)
  const generation = useRef(0)
  const stream = useRef<MediaStream | null>(null)
  const context = useRef<AudioContext | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)
  const request = useRef<AbortController | null>(null)
  const callback = useRef(onTranscript)
  callback.current = onTranscript

  const stop = useCallback(() => {
    active.current = false
    generation.current++
    request.current?.abort()
    request.current = null
    if (timer.current) clearInterval(timer.current)
    timer.current = null
    if (recorder.current) {
      recorder.current.onstop = null
      recorder.current.ondataavailable = null
      if (recorder.current.state !== 'inactive') recorder.current.stop()
    }
    recorder.current = null
    stream.current?.getTracks().forEach((track) => track.stop())
    stream.current = null
    void context.current?.close().catch(() => {})
    context.current = null
    setLevel(0)
    setState('off')
  }, [])

  const toggleMute = useCallback(() => {
    muted.current = !muted.current
    stream.current?.getAudioTracks().forEach((track) => { track.enabled = !muted.current })
    // Discard an unfinished utterance when muting.
    if (muted.current && recorder.current?.state === 'recording') {
      recorder.current.onstop = null
      recorder.current.stop()
      recorder.current = null
    }
    setLevel(0)
    setState(muted.current ? 'muted' : 'listening')
  }, [])

  const start = useCallback(async () => {
    if (active.current) return
    active.current = true
    muted.current = false
    const session = ++generation.current
    const isCurrent = () => active.current && generation.current === session
    const controller = new AbortController()
    request.current = controller
    setError(null)
    setState('starting')
    try {
      const statusResponse = await fetch(`${API_BASE_URL}/api/stt/status`, { signal: controller.signal })
      if (!statusResponse.ok) throw new Error('Could not check speech availability. Try again or type a message.')
      const status = await statusResponse.json() as { enabled: boolean; message?: string; max_duration_seconds?: number }
      if (!status.enabled) throw new Error(status.message || 'Speech input is disabled. You can still type a message.')
      if (!isCurrent()) return
      const capture = await navigator.mediaDevices.getUserMedia({ audio: {
        ...(microphoneId ? { deviceId: { exact: microphoneId } } : {}),
        echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      } })
      if (!isCurrent()) {
        capture.getTracks().forEach((track) => track.stop())
        return
      }
      stream.current = capture
      const audioContext = new AudioContext()
      context.current = audioContext
      await audioContext.resume()
      if (!isCurrent()) return
      const analyser = audioContext.createAnalyser()
      analyser.fftSize = 1024
      audioContext.createMediaStreamSource(capture).connect(analyser)
      const samples = new Float32Array(analyser.fftSize)
      let busy = false
      let startedAt = 0
      let lastVoiceAt = 0
      let voicedMs = 0
      let quietUntil = 0
      const maxMs = Math.min(60, status.max_duration_seconds || 60) * 1000 - 500
      capture.getAudioTracks()[0].onended = () => {
        if (!isCurrent()) return
        stop()
        setError('Microphone disconnected. Select a microphone and start again.')
      }
      setState('listening')
      timer.current = setInterval(() => {
        if (!isCurrent()) return
        const now = performance.now()
        const store = useChatStore.getState()
        const agentBusy = store.isAgentResponding || store.isAISpeaking || store.audioQueue.length > 0
        if (agentBusy || busy || muted.current) {
          quietUntil = now + 500
          setLevel(0)
          if (!busy) setState(muted.current ? 'muted' : 'waiting')
          return
        }
        if (now < quietUntil) return
        analyser.getFloatTimeDomainData(samples)
        const rms = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length)
        setLevel(Math.min(1, rms * 12))
        const speaking = rms > 0.015
        if (speaking) {
          lastVoiceAt = now
          if (!recorder.current) {
            const chunks: BlobPart[] = []
            const recording = new MediaRecorder(capture)
            recorder.current = recording
            startedAt = now
            voicedMs = 0
            recording.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data) }
            recording.onerror = () => {
              if (!isCurrent()) return
              stop()
              setError('Audio recording failed. Start again or type a message.')
            }
            recording.onstop = async () => {
              recorder.current = null
              if (!isCurrent()) return
              if (voicedMs < 200) { setState('listening'); return }
              busy = true
              setState('transcribing')
              try {
                const form = new FormData()
                const blob = new Blob(chunks, { type: recording.mimeType })
                form.append('audio', blob, 'conversation.webm')
                form.append('mime_type', blob.type)
                const response = await fetch(`${API_BASE_URL}/api/stt`, {
                  method: 'POST', body: form, signal: controller.signal,
                })
                const payload = await response.json() as { text?: string; detail?: string }
                if (!isCurrent()) return
                if (!response.ok) throw new Error(payload.detail || 'Transcription failed. Start again or type a message.')
                if (payload.text?.trim() && !callback.current(payload.text)) {
                  throw new Error('Connection lost. Start again when the agent reconnects.')
                }
                setState(muted.current ? 'muted' : payload.text?.trim() ? 'waiting' : 'listening')
              } catch (failure) {
                if (!isCurrent()) return
                stop()
                setError(failure instanceof Error ? failure.message : 'Voice conversation failed.')
              } finally {
                busy = false
                quietUntil = performance.now() + 500
              }
            }
            recording.start()
            setState('recording')
          }
          voicedMs += 50
        }
        const recording = recorder.current
        if (recording?.state === 'recording' && (now - lastVoiceAt >= 1000 || now - startedAt >= maxMs)) {
          recording.stop()
        } else if (!recording) {
          setState('listening')
        }
      }, 50)
    } catch (failure) {
      if (!isCurrent()) return
      stop()
      setError(failure instanceof Error ? failure.message : 'Microphone access failed. You can still type a message.')
    }
  }, [microphoneId, stop])

  useEffect(() => {
    const unsubscribe = useChatStore.subscribe((next, previous) => {
      if (next.wsStatus !== 'open' || next.activeSessionId !== previous.activeSessionId ||
        next.currentCharacterId !== previous.currentCharacterId || !next.ttsEnabled) stop()
    })
    const onVisibility = () => { if (document.hidden) stop() }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      unsubscribe()
      document.removeEventListener('visibilitychange', onVisibility)
      stop()
    }
  }, [stop])

  return { state, error, level, start, stop, toggleMute, active: state !== 'off' }
}
