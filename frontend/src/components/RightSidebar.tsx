import { Suspense, lazy, useEffect, useRef, useState } from 'react'
import { useChatStore } from '../store/chatStore'
import { ErrorBoundary } from './ErrorBoundary'
import { API_BASE_URL } from '../config/backend'

const Scene = lazy(() => import('./Scene').then((module) => ({ default: module.Scene })))

type AssetDropdownProps = {
  label: string
  value: string
  options: string[]
  fallbackLabel: string
  getOptionValue: (option: string) => string
  getOptionLabel?: (option: string) => string
  onChange: (value: string) => void
}

function AssetDropdown({
  label,
  value,
  options,
  fallbackLabel,
  getOptionValue,
  getOptionLabel = (option) => option,
  onChange,
}: AssetDropdownProps) {
  const [open, setOpen] = useState(false)
  const selectedOption = options.find((option) => getOptionValue(option) === value)
  const displayLabel = (selectedOption ? getOptionLabel(selectedOption) : fallbackLabel) || `Choose ${label.toLowerCase()}...`
  const listId = `${label.toLowerCase().replace(/\s+/g, '-')}-asset-list`

  return (
    <div style={{ ...s.controlRow, zIndex: open ? 100 : 1 }}>
      <span style={s.label}>{label}</span>
      <div style={s.dropdown}>
        <button
          type="button"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => setOpen((current) => !current)}
          onBlur={() => window.setTimeout(() => setOpen(false), 120)}
          style={s.dropdownButton}
        >
          <span style={s.dropdownText}>{displayLabel}</span>
          <span aria-hidden="true" style={s.chevron}>⌄</span>
        </button>
        {open && (
          <div id={listId} role="listbox" style={s.dropdownMenu}>
            {(options.length > 0 ? options : [fallbackLabel]).map((option) => {
              const optionValue = options.length > 0 ? getOptionValue(option) : value
              const optionLabel = options.length > 0 ? getOptionLabel(option) : fallbackLabel
              const selected = optionValue === value

              return (
                <button
                  key={optionValue}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    onChange(optionValue)
                    setOpen(false)
                  }}
                  style={{
                    ...s.dropdownOption,
                    ...(selected ? s.dropdownOptionSelected : null),
                  }}
                >
                  {optionLabel}
                </button>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

export function RightSidebar() {
  const { currentModel, currentVoice, authToken, characters, currentCharacterId, setCurrentModel, setCurrentVoice } = useChatStore()
  const [models, setModels] = useState<string[]>([])
  const [voices, setVoices] = useState<string[]>([])
  const [supportsVoiceCloning, setSupportsVoiceCloning] = useState(true)
  const [cloneReady, setCloneReady] = useState(false)
  const [cloneStatus, setCloneStatus] = useState('loading')
  const [voiceError, setVoiceError] = useState<string | null>(null)
  const [recordingVoice, setRecordingVoice] = useState(false)
  const [recordingSeconds, setRecordingSeconds] = useState(0)
  const [previewingVoice, setPreviewingVoice] = useState(false)
  const voiceRecorderRef = useRef<MediaRecorder | null>(null)
  const voiceCaptureEpochRef = useRef(0)
  const voiceStreamRef = useRef<MediaStream | null>(null)
  const voiceTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const previewRef = useRef<HTMLAudioElement | null>(null)
  const previewUrlRef = useRef<string | null>(null)
  const [customModelName, setCustomModelName] = useState<string | null>(null)
  const [customModelFile, setCustomModelFile] = useState<File | null>(null)
  const [uploadingVoice, setUploadingVoice] = useState(false)
  const [uploadingAvatar, setUploadingAvatar] = useState(false)

  const currentCharacter = characters.find((c) => c.id === currentCharacterId)

  useEffect(() => {
    // Fetch models
    fetch(`${API_BASE_URL}/api/avatars`)
      .then((res) => res.json())
      .then((data) => {
        if (data.avatars && data.avatars.length > 0) {
          setModels(data.avatars)
        }
      })
      .catch((err) => console.error('Failed to fetch models', err))

    // Fetch voices
    const headers: HeadersInit = authToken ? { Authorization: `Bearer ${authToken}` } : {}
    fetch(`${API_BASE_URL}/api/voices`, { headers })
      .then((res) => {
        if (res.status === 401) {
          useChatStore.getState().logout()
          throw new Error('Unauthorized')
        }
        return res.json()
      })
      .then((data) => {
          setSupportsVoiceCloning(data.supports_voice_cloning !== false)
          setCloneReady(data.clone_ready === true)
          setCloneStatus(data.clone_status || 'loading')
        if (data.voices && data.voices.length > 0) {
          setVoices(data.voices)
            if (['kokoro', 'chatterbox-nano'].includes(data.provider) && !data.voices.includes(useChatStore.getState().currentVoice)) {
              setCurrentVoice('')
            }
        }
      })
      .catch((err) => console.error('Failed to fetch voices', err))
  }, [authToken])

  const handleModelUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) {
      const url = URL.createObjectURL(file)
      setCustomModelName(file.name)
      setCustomModelFile(file)
      setCurrentModel(url)
    }
  }

  const handleSaveAvatarForCharacter = async () => {
    if (!customModelFile || !currentCharacterId) return
    setUploadingAvatar(true)
    const formData = new FormData()
    formData.append('file', customModelFile)
    const headers: HeadersInit = authToken ? { Authorization: `Bearer ${authToken}` } : {}

    try {
      const res = await fetch(`${API_BASE_URL}/api/characters/${currentCharacterId}/avatar`, {
        method: 'POST',
        headers,
        body: formData,
      })
      if (res.status === 401) {
        useChatStore.getState().logout()
        return
      }
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      // Replace the local blob URL with the persisted server URL so it
      // survives beyond this browser tab's lifetime.
      if (data.avatar) setCurrentModel(data.avatar)
      setCustomModelFile(null)
    } catch (err) {
      console.error('Failed to save custom avatar', err)
      alert('Failed to save avatar: ' + (err instanceof Error ? err.message : String(err)))
    } finally {
      setUploadingAvatar(false)
    }
  }

  const uploadVoiceFile = async (file: File) => {
    setUploadingVoice(true)
    setVoiceError(null)
    const formData = new FormData()
    formData.append('file', file)

    const headers: HeadersInit = authToken ? { Authorization: `Bearer ${authToken}` } : {}

    try {
      const res = await fetch(`${API_BASE_URL}/api/voices/upload`, {
        method: 'POST',
        headers,
        body: formData,
      })
      if (res.status === 401) {
        useChatStore.getState().logout()
        return
      }
      if (!res.ok) {
        const failure = await res.json().catch(() => null) as { detail?: string } | null
        throw new Error(failure?.detail || 'Voice upload failed.')
      }
      const data = await res.json()

      // Refresh voices list
      const voicesRes = await fetch(`${API_BASE_URL}/api/voices`, { headers: headers })
      if (voicesRes.status === 401) {
        useChatStore.getState().logout()
        return
      }
      const voicesData = await voicesRes.json()
      setCloneReady(voicesData.clone_ready === true)
      setCloneStatus(voicesData.clone_status || 'loading')
      if (voicesData.voices) {
        setVoices(voicesData.voices)
        setCurrentVoice(data.filename)
      }
    } catch (err) {
      console.error('Failed to upload voice', err)
      setVoiceError(err instanceof Error ? err.message : 'Voice upload failed.')
    } finally {
      setUploadingVoice(false)
    }
  }

  const handleVoiceUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) void uploadVoiceFile(file)
    e.target.value = ''
  }

  const stopVoiceRecording = () => {
    voiceCaptureEpochRef.current += 1
    if (voiceTimerRef.current) clearInterval(voiceTimerRef.current)
    voiceTimerRef.current = null
    if (voiceRecorderRef.current?.state === 'recording') voiceRecorderRef.current.stop()
    else setRecordingVoice(false)
  }

  const recordVoice = async () => {
    if (recordingVoice || voiceRecorderRef.current) return
    setVoiceError(null)
    setRecordingVoice(true)
    setRecordingSeconds(0)
    const epoch = ++voiceCaptureEpochRef.current
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (epoch !== voiceCaptureEpochRef.current) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      voiceStreamRef.current = stream
      const recorder = new MediaRecorder(stream)
      voiceRecorderRef.current = recorder
      const chunks: BlobPart[] = []
      recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data) }
      recorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop())
        voiceStreamRef.current = null
        voiceRecorderRef.current = null
        setRecordingVoice(false)
        const extension = recorder.mimeType.includes('mp4') ? 'mp4' : 'webm'
        void uploadVoiceFile(new File(chunks, `my-voice.${extension}`, { type: recorder.mimeType }))
      }
      recorder.start()
      let seconds = 0
      voiceTimerRef.current = setInterval(() => {
        seconds += 1
        setRecordingSeconds(seconds)
        if (seconds >= 12) stopVoiceRecording()
      }, 1000)
    } catch (error) {
      voiceStreamRef.current?.getTracks().forEach((track) => track.stop())
      voiceStreamRef.current = null
      voiceRecorderRef.current = null
      setRecordingVoice(false)
      setVoiceError(error instanceof Error ? error.message : 'Microphone access failed.')
    }
  }

  const previewVoice = async () => {
    setVoiceError(null)
    setPreviewingVoice(true)
    try {
      const response = await fetch(`${API_BASE_URL}/api/voices/preview`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${authToken}` },
        body: JSON.stringify({ voice: currentVoice || currentCharacter?.voice || 'af_bella' }),
      })
      if (!response.ok) {
        const failure = await response.json().catch(() => null) as { detail?: string } | null
        throw new Error(failure?.detail || 'Voice preview failed.')
      }
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
      const url = URL.createObjectURL(await response.blob())
      previewUrlRef.current = url
      const audio = new Audio(url)
      previewRef.current = audio
      audio.onended = () => { setPreviewingVoice(false); URL.revokeObjectURL(url); previewUrlRef.current = null }
      audio.onerror = () => { setPreviewingVoice(false); setVoiceError('Could not play the voice preview.') }
      await audio.play()
    } catch (error) {
      setPreviewingVoice(false)
      setVoiceError(error instanceof Error ? error.message : 'Voice preview failed.')
    }
  }

  useEffect(() => () => {
    voiceCaptureEpochRef.current += 1
    if (voiceTimerRef.current) clearInterval(voiceTimerRef.current)
    const recorder = voiceRecorderRef.current
    if (recorder) {
      recorder.onstop = null
      if (recorder.state !== 'inactive') recorder.stop()
    }
    voiceStreamRef.current?.getTracks().forEach((track) => track.stop())
    previewRef.current?.pause()
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
  }, [])

  useEffect(() => {
    if (!supportsVoiceCloning || cloneReady || !authToken) return
    const timer = setInterval(() => {
      void fetch(`${API_BASE_URL}/api/voices`, { headers: { Authorization: `Bearer ${authToken}` } })
        .then((response) => response.ok ? response.json() : null)
        .then((data) => {
          if (data?.clone_status) setCloneStatus(data.clone_status)
          if (data?.clone_ready) setCloneReady(true)
        })
        .catch(() => {})
    }, 10000)
    return () => clearInterval(timer)
  }, [supportsVoiceCloning, cloneReady, authToken])

  return (
    <div style={s.sidebar}>
      {/* Settings Panel */}
      <div style={s.settingsPanel}>
        <div style={s.header}>
          <span style={s.title}>Avatar & Voice Options</span>
        </div>
        
        <div style={s.divider} />
        
        <AssetDropdown
          label="3D Model"
          value={currentModel}
          options={models}
          fallbackLabel={customModelName || (currentModel.split('/').pop() ?? currentModel)}
          getOptionValue={(model) => `/models/${model}`}
          onChange={(val) => {
            setCurrentModel(val)
            setCustomModelName(null)
          }}
        />

        <div style={s.uploadRow}>
          <label style={s.uploadBtn}>
            📁 Choose local .glb model
            <input
              type="file"
              accept=".glb"
              onChange={handleModelUpload}
              style={{ display: 'none' }}
            />
          </label>
          {customModelName && <span style={s.fileName}>{customModelName}</span>}
        </div>

        {customModelFile && currentCharacter && (
          <div style={s.uploadRow}>
            <button
              type="button"
              onClick={handleSaveAvatarForCharacter}
              disabled={uploadingAvatar}
              style={s.saveAvatarBtn}
            >
              {uploadingAvatar ? 'Saving...' : `💾 Save as ${currentCharacter.display_name}'s avatar`}
            </button>
          </div>
        )}

        <AssetDropdown
          label="Voice"
          value={currentVoice}
          options={voices}
          fallbackLabel={currentVoice}
          getOptionValue={(voice) => voice}
          getOptionLabel={(voice) => (voice.split('/').pop() || voice).replace(/-[0-9a-f]{12}\.wav$/i, '').replace(/\.wav$/i, '')}
          onChange={setCurrentVoice}
        />

          {supportsVoiceCloning && <div style={s.uploadRow}>
          <label style={s.uploadBtn}>
            🎙️ Upload voice recording
            <input
              type="file"
              accept=".wav,.mp3,.webm,.m4a"
              disabled={uploadingVoice || recordingVoice}
              onChange={handleVoiceUpload}
              style={{ display: 'none' }}
            />
          </label>
          {uploadingVoice && <span style={s.fileName}>Uploading...</span>}
          </div>}

        {supportsVoiceCloning && (
          <div style={{ color: '#94a3b8', fontSize: 11, marginTop: 8 }}>
            <p>Use 10–15 seconds of clear English speech. Record once and reuse the voice.</p>
            {typeof MediaRecorder !== 'undefined' && (
              <button style={s.uploadBtn} disabled={uploadingVoice} onClick={() => recordingVoice ? stopVoiceRecording() : void recordVoice()}>
                {recordingVoice ? `Stop recording · ${recordingSeconds}/12s` : 'Record my voice (12 seconds)'}
              </button>
            )}
            {!cloneReady && <p>{cloneStatus === 'loading' ? 'The cloning model is warming up.' : 'Voice cloning is currently unavailable.'} Preset voices remain available.</p>}
          </div>
        )}
        <button style={{ ...s.uploadBtn, marginTop: 8 }} disabled={previewingVoice || uploadingVoice || recordingVoice} onClick={() => void previewVoice()}>
          {previewingVoice ? 'Playing / preparing preview…' : 'Preview voice'}
        </button>
        {voiceError && <p style={{ color: '#fca5a5', fontSize: 11 }}>{voiceError}</p>}

        <div style={s.divider} />
      </div>

      {/* 3D Scene Container */}
      <div style={s.sceneContainer}>
        <ErrorBoundary fallback={<div style={s.sceneLoading}><div style={s.sceneLoadingText}>3D Avatar Unavailable</div></div>}>
          <Suspense fallback={<SceneLoading />}>
            <Scene />
          </Suspense>
        </ErrorBoundary>
      </div>
    </div>
  )
}

function SceneLoading() {
  return (
    <div style={s.sceneLoading}>
      <div style={s.sceneLoadingMark}>◇</div>
      <div style={s.sceneLoadingText}>Loading avatar</div>
    </div>
  )
}

const s: Record<string, React.CSSProperties> = {
  sidebar: {
    width: 320,
    minWidth: 320,
    height: '100%',
    display: 'flex',
    flexDirection: 'column',
    background: '#151521',
    borderLeft: '1px solid rgba(255,255,255,0.07)',
    fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
  },
  settingsPanel: {
    padding: '16px 14px',
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
  },
  title: {
    fontSize: 14,
    fontWeight: 600,
    color: '#e2e8f0',
    letterSpacing: '0.02em',
  },
  divider: {
    height: 1,
    background: 'rgba(255,255,255,0.06)',
  },
  controlRow: {
    position: 'relative',
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
  },
  label: {
    fontSize: 11,
    fontWeight: 600,
    color: '#6b7280',
    textTransform: 'uppercase',
    letterSpacing: '0.05em',
  },
  dropdown: {
    position: 'relative',
  },
  dropdownButton: {
    width: '100%',
    height: 36,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    background: 'rgba(255,255,255,0.05)',
    color: '#e2e8f0',
    border: '1px solid rgba(255,255,255,0.08)',
    borderRadius: 6,
    padding: '0 8px 0 14px',
    fontSize: 12,
    cursor: 'pointer',
    outline: 'none',
    fontFamily: 'inherit',
  },
  dropdownText: {
    minWidth: 0,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    fontWeight: 600,
    textAlign: 'left',
  },
  chevron: {
    flex: '0 0 auto',
    color: '#cbd5e1',
    fontSize: 18,
    lineHeight: 1,
    paddingLeft: 8,
  },
  dropdownMenu: {
    position: 'absolute',
    top: 'calc(100% + 4px)',
    left: 0,
    right: 0,
    zIndex: 1000,
    maxHeight: 168,
    overflowY: 'auto',
    padding: 4,
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    background: '#20202c',
    border: '1px solid rgba(255,255,255,0.12)',
    borderRadius: 6,
    boxShadow: '0 14px 32px rgba(0,0,0,0.38)',
  },
  dropdownOption: {
    width: '100%',
    minHeight: 32,
    border: 0,
    borderRadius: 4,
    padding: '7px 10px',
    background: 'transparent',
    color: '#dbeafe',
    cursor: 'pointer',
    fontFamily: 'inherit',
    fontSize: 12,
    fontWeight: 600,
    textAlign: 'left',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  dropdownOptionSelected: {
    background: 'rgba(96,165,250,0.2)',
    color: '#ffffff',
  },
  sceneContainer: {
    flex: 1,
    position: 'relative',
    background: 'linear-gradient(180deg, #0d1117 0%, #161b27 100%)',
    overflow: 'hidden',
  },
  sceneLoading: {
    position: 'absolute',
    inset: 0,
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 8,
    color: '#94a3b8',
    background: 'linear-gradient(180deg, #0d1117 0%, #161b27 100%)',
  },
  sceneLoadingMark: {
    fontSize: 28,
    color: '#60a5fa',
  },
  sceneLoadingText: {
    fontSize: 11,
    fontWeight: 600,
    letterSpacing: '0.04em',
    textTransform: 'uppercase',
  },
  uploadRow: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 8,
    marginTop: -4,
    marginBottom: 10,
    paddingLeft: 10,
    paddingRight: 10,
  },
  uploadBtn: {
    display: 'inline-flex',
    alignItems: 'center',
    background: 'rgba(255,255,255,0.03)',
    border: '1px dashed rgba(255,255,255,0.18)',
    borderRadius: 6,
    padding: '5px 10px',
    color: '#94a3b8',
    fontSize: 11,
    fontWeight: 600,
    cursor: 'pointer',
    transition: 'all 0.2s ease',
  },
  fileName: {
    color: '#60a5fa',
    fontSize: 10,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    maxWidth: 145,
    fontStyle: 'italic',
  },
  saveAvatarBtn: {
    width: '100%',
    background: 'rgba(96,165,250,0.12)',
    border: '1px solid rgba(96,165,250,0.35)',
    borderRadius: 6,
    padding: '7px 10px',
    color: '#93c5fd',
    fontSize: 11,
    fontWeight: 700,
    cursor: 'pointer',
    fontFamily: 'inherit',
  },
}
