import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { API_BASE_URL } from '../config/backend'

type SpeechState = 'idle' | 'listening' | 'recording' | 'processing' | 'uploading' | 'error'

interface UseSpeechInputOptions {
  onTranscript: (text: string) => void
  pauseVAD: () => void
  resumeVAD: () => void
  getStream: () => MediaStream | null
  maxRecordingSeconds: number
}

interface SpeechInputResult {
  supported: boolean
  state: SpeechState
  mode: 'backend' | 'browser'
  permissionDenied: boolean
  errorMessage: string | null
  micLevel: number
  microphones: MediaDeviceInfo[]
  microphoneId: string
  setMicrophoneId: (id: string) => void
  remainingSeconds: number
  startRecording: () => void
  stopRecording: () => void
}

export function useSpeechInput({
  onTranscript,
  pauseVAD,
  resumeVAD,
  maxRecordingSeconds,
}: UseSpeechInputOptions): SpeechInputResult {
  const [state, setState] = useState<SpeechState>('idle')
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [permissionDenied, setPermissionDenied] = useState(false)
  const [remainingSeconds, setRemainingSeconds] = useState(maxRecordingSeconds)
  const [micLevel, setMicLevel] = useState(0)
  const [microphones, setMicrophones] = useState<MediaDeviceInfo[]>([])
  const [microphoneId, setMicrophoneId] = useState('')
  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const meterContextRef = useRef<AudioContext | null>(null)
  const meterFrameRef = useRef<number | null>(null)
  const chunksRef = useRef<BlobPart[]>([])
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const supported = typeof MediaRecorder !== 'undefined' && !!navigator.mediaDevices?.getUserMedia

  const refreshMicrophones = useCallback(async () => {
    if (!navigator.mediaDevices?.enumerateDevices) return
    try {
      const devices = await navigator.mediaDevices.enumerateDevices()
      setMicrophones(devices.filter((device) => device.kind === 'audioinput' && device.deviceId))
    } catch {
      // Recording can still work if device enumeration is unavailable.
    }
  }, [])

  const releaseCapture = useCallback(() => {
    if (meterFrameRef.current !== null) cancelAnimationFrame(meterFrameRef.current)
    meterFrameRef.current = null
    void meterContextRef.current?.close().catch(() => {})
    meterContextRef.current = null
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
  }, [])

  const cleanupTimer = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current)
      timerRef.current = null
    }
  }, [])

  const uploadAudio = useCallback(async (blob: Blob) => {
    setState('uploading')
    try {
      const form = new FormData()
      form.append('audio', blob, 'speech.webm')
      form.append('mime_type', blob.type || 'audio/webm')
      const response = await fetch(`${API_BASE_URL}/api/stt`, {
        method: 'POST',
        body: form,
      })
      if (!response.ok) {
        const failure = await response.json().catch(() => null) as { detail?: string } | null
        throw new Error(failure?.detail || `STT failed: ${response.status}`)
      }
      const payload = (await response.json()) as { text?: string; message?: string }
      if (!payload.text?.trim()) throw new Error(payload.message || 'No speech detected. Please try again.')
      onTranscript(payload.text)
      setState('idle')
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Voice input failed')
      setState('error')
    } finally {
      resumeVAD()
    }
  }, [onTranscript, resumeVAD])

  const stopRecording = useCallback(() => {
    cleanupTimer()
    const recorder = recorderRef.current
    if (recorder && recorder.state !== 'inactive') {
      recorder.stop()
    }
  }, [cleanupTimer])

  const startRecording = useCallback(async () => {
    if (!supported) return
    setErrorMessage(null)
    setPermissionDenied(false)
    try {
      setState('processing')
      const response = await fetch(`${API_BASE_URL}/api/stt/status`)
      if (!response.ok) throw new Error('Could not check voice input availability.')
      const status = await response.json() as { enabled: boolean; message?: string }
      if (!status.enabled) {
        throw new Error(status.message || 'Voice input is disabled on the backend. You can still type a message.')
      }
      pauseVAD()
      // Acquire a fresh recording stream; the background VAD stream may be stale.
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          ...(microphoneId ? { deviceId: { exact: microphoneId } } : {}),
          autoGainControl: true,
          echoCancellation: true,
          noiseSuppression: false,
        },
      })
      streamRef.current = stream
      await refreshMicrophones()
      const track = stream.getAudioTracks()[0]
      if (!track || track.readyState !== 'live' || !track.enabled) {
        throw new Error('Microphone is unavailable. Select another microphone and try again.')
      }
      // Meter failures should not prevent MediaRecorder from capturing audio.
      try {
        const context = new AudioContext()
        meterContextRef.current = context
        await context.resume()
        const analyser = context.createAnalyser()
        analyser.fftSize = 1024
        context.createMediaStreamSource(stream).connect(analyser)
        const samples = new Float32Array(analyser.fftSize)
        const updateMeter = () => {
          analyser.getFloatTimeDomainData(samples)
          const rms = Math.sqrt(samples.reduce((sum, sample) => sum + sample * sample, 0) / samples.length)
          setMicLevel(Math.min(1, rms * 12))
          meterFrameRef.current = requestAnimationFrame(updateMeter)
        }
        updateMeter()
      } catch {
        void meterContextRef.current?.close().catch(() => {})
        meterContextRef.current = null
      }
      chunksRef.current = []
      const recorder = new MediaRecorder(stream)
      recorderRef.current = recorder
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data)
      }
      recorder.onstop = () => {
        cleanupTimer()
        releaseCapture()
        setMicLevel(0)
        recorderRef.current = null
        setState('processing')
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
        void uploadAudio(blob)
      }
      setRemainingSeconds(maxRecordingSeconds)
      timerRef.current = setInterval(() => {
        setRemainingSeconds((current) => {
          if (current <= 1) {
            stopRecording()
            return 0
          }
          return current - 1
        })
      }, 1000)
      recorder.start()
      setState('recording')
    } catch (error) {
      cleanupTimer()
      releaseCapture()
      setMicLevel(0)
      resumeVAD()
      setPermissionDenied(error instanceof DOMException && error.name === 'NotAllowedError')
      setErrorMessage(error instanceof Error ? error.message : 'Microphone access failed')
      setState('error')
    }
  }, [cleanupTimer, maxRecordingSeconds, microphoneId, pauseVAD, refreshMicrophones, releaseCapture, resumeVAD, stopRecording, supported, uploadAudio])

  useEffect(() => {
    void refreshMicrophones()
    navigator.mediaDevices?.addEventListener('devicechange', refreshMicrophones)
    return () => {
      navigator.mediaDevices?.removeEventListener('devicechange', refreshMicrophones)
      const recorder = recorderRef.current
      if (recorder) {
        recorder.onstop = null
        recorder.ondataavailable = null
        if (recorder.state !== 'inactive') recorder.stop()
      }
      cleanupTimer()
      releaseCapture()
    }
  }, [cleanupTimer, refreshMicrophones, releaseCapture])

  return useMemo(() => ({
    supported,
    state,
    mode: 'backend',
    permissionDenied,
    errorMessage,
    micLevel,
    microphones,
    microphoneId,
    setMicrophoneId,
    remainingSeconds,
    startRecording,
    stopRecording,
  }), [
    errorMessage,
    micLevel,
    microphones,
    microphoneId,
    permissionDenied,
    remainingSeconds,
    startRecording,
    state,
    stopRecording,
    supported,
  ])
}
