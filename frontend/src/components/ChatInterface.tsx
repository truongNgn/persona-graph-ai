/**
 * ChatInterface - message history + input bar.
 * Status bar and LLM dropdown live in Sidebar.
 */

import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { API_BASE_URL } from '../config/backend'
import { useSpeechInput } from '../hooks/useSpeechInput'
import { useLiveConversation } from '../hooks/useLiveConversation'
import './ChatInterface.css'
import { useChatStore } from '../store/chatStore'
import { CharacterPicker } from './CharacterPicker'
import type { Emotion } from '../types'

interface ChatInterfaceProps {
  sendMessage: (text: string) => void
  sendInterrupt: () => void
  preparePlayback: () => Promise<AudioContext | null>
}

const EMOTION_COLORS: Record<Emotion, string> = {
  joy: '#fbbf24',
  sad: '#60a5fa',
  neutral: '#9ca3af',
  thinking: '#a78bfa',
  surprise: '#f472b6',
  anger: '#f87171',
}

export function ChatInterface({
  sendMessage,
  sendInterrupt,
  preparePlayback,
}: ChatInterfaceProps) {
  const [inputText, setInputText] = useState('')
  const [voiceMode, setVoiceMode] = useState<'record' | 'live'>('record')
  const messagesEndRef = useRef<HTMLDivElement>(null)

  const {
    messages,
    wsStatus,
    isAISpeaking,
    isAgentResponding,
    setTtsEnabled,
    currentEmotion,
    ttsEnabled,
    autoSendVoiceTranscript,
    warmupStatus,
    userId,
    authToken,
    activeSessionId,
    setMessageFeedback,
  } = useChatStore()

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length])

  const sendUserText = useCallback((rawText: string): boolean => {
    const text = rawText.trim()
    if (!text || wsStatus !== 'open') return false
    useChatStore.getState().addMessage({ id: crypto.randomUUID(), role: 'user', text })
    sendMessage(text)
    return true
  }, [wsStatus, sendMessage])

  const handleSend = useCallback(() => {
    if (sendUserText(inputText)) {
      setInputText('')
    }
  }, [inputText, sendUserText])

  const handleVoiceTranscript = useCallback((text: string) => {
    if (autoSendVoiceTranscript) {
      if (sendUserText(text)) {
        setInputText('')
      }
      return
    }
    setInputText(text)
  }, [autoSendVoiceTranscript, sendUserText])

  const speechInput = useSpeechInput({
    onTranscript: handleVoiceTranscript,
    maxRecordingSeconds: 60,
  })
  const live = useLiveConversation(sendUserText, speechInput.microphoneId)
  const voiceInputBusy = (
    speechInput.state === 'listening' ||
    speechInput.state === 'recording' ||
    speechInput.state === 'processing' ||
    speechInput.state === 'uploading'
  )

  const handleMicClick = useCallback(() => {
    if (wsStatus !== 'open' || !speechInput.supported) return

    if (voiceInputBusy) {
      speechInput.stopRecording()
      return
    }

    if (isAISpeaking) {
      sendInterrupt()
    }

    speechInput.startRecording()
  }, [isAISpeaking, sendInterrupt, speechInput, voiceInputBusy, wsStatus])

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        handleSend()
      }
    },
    [handleSend],
  )

  const submitFeedback = useCallback(async (messageId: string, turnId: string, rating: 'up' | 'down') => {
    setMessageFeedback(messageId, rating)
    try {
      const response = await fetch(`${API_BASE_URL}/api/feedback/rating`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        },
        body: JSON.stringify({
          user_id: userId,
          session_id: activeSessionId,
          turn_id: turnId,
          rating,
          message_id: messageId,
        }),
      })
      if (response.status === 401) {
        useChatStore.getState().logout()
        return
      }
      if (!response.ok) {
        throw new Error(`Rating failed: ${response.status}`)
      }
    } catch (error) {
      console.error('[Feedback] Failed to submit rating:', error)
    }
  }, [activeSessionId, authToken, setMessageFeedback, userId])

  const isEmpty = messages.length === 0
  const warmupRunning = warmupStatus?.status === 'running'
  const micDisabled = wsStatus !== 'open' || !speechInput.supported || speechInput.state === 'processing' || speechInput.state === 'uploading'
  const voiceStateLabels: Record<string, string> = {
    idle: '',
    listening: 'Listening',
    recording: 'Recording',
    processing: 'Processing',
    uploading: 'Transcribing',
    error: 'Voice input error',
  }
  const voiceStateLabel = voiceStateLabels[speechInput.state] ?? ''
  const micTitle = !speechInput.supported
    ? 'Voice input is not supported'
    : speechInput.permissionDenied
      ? 'Microphone access denied'
    : speechInput.errorMessage ?? `${speechInput.mode === 'backend' ? 'Backend STT' : 'Browser speech'} · ${autoSendVoiceTranscript ? 'Auto send' : 'Review transcript'}`

  return (
    <div style={s.container}>
      {/* ── Header: current character + switcher ─────────── */}
      <div style={s.header}>
        <CharacterPicker />
      </div>

      {/* ── Message list / Empty state ─────────────────── */}
      <div style={s.messageList}>
        {isEmpty ? (
          <div style={s.emptyState}>
            <div style={s.emptyIcon}>◈</div>
            <div style={s.emptyTitle}>AI Chatbot</div>
            <div style={s.emptySubtitle}>Hello. What would you like to explore today?</div>
          </div>
        ) : (
          messages.map((msg) => (
            <div
              key={msg.id}
              style={{
                ...s.messageBubble,
                alignSelf: msg.role === 'user' ? 'flex-end' : 'flex-start',
                background:
                  msg.role === 'user'
                    ? 'rgba(79,70,229,0.72)'
                    : 'rgba(22,30,46,0.82)',
                borderLeft:
                  msg.role === 'assistant' && msg.emotion
                    ? `3px solid ${EMOTION_COLORS[msg.emotion]}`
                    : msg.role === 'assistant'
                      ? '3px solid rgba(255,255,255,0.08)'
                      : 'none',
              }}
            >
              {msg.text}
              {msg.role === 'assistant' && msg.turnId && (
                <div style={s.feedbackActions}>
                  <button
                    onClick={() => submitFeedback(msg.id, msg.turnId!, 'up')}
                    style={{
                      ...s.feedbackBtn,
                      ...(msg.feedbackRating === 'up' ? s.feedbackBtnActive : null),
                    }}
                    title="Good response"
                  >
                    ▲
                  </button>
                  <button
                    onClick={() => submitFeedback(msg.id, msg.turnId!, 'down')}
                    style={{
                      ...s.feedbackBtn,
                      ...(msg.feedbackRating === 'down' ? s.feedbackBtnActive : null),
                    }}
                    title="Bad response"
                  >
                    ▼
                  </button>
                </div>
              )}
            </div>
          ))
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* ── TTS-off badge ───────────────────────────────── */}
      {!ttsEnabled && (
        <div style={s.ttsBadge}>Text only - faster response</div>
      )}

      {/* ── Input area ──────────────────────────────────── */}
      <div style={s.inputWrap}>
        <div className="voice-mode-picker" role="group" aria-label="Voice input mode">
          <button aria-pressed={voiceMode === 'record'} disabled={voiceInputBusy || live.active}
            onClick={() => setVoiceMode('record')}>Record a message</button>
          <button aria-pressed={voiceMode === 'live'} disabled={voiceInputBusy || live.active}
            onClick={() => setVoiceMode('live')}>Live conversation</button>
        </div>
        {voiceMode === 'live' && (
          <div className={`live-conversation ${live.active ? 'is-active' : ''}`}>
            <div className="live-conversation-heading">
              <div>
                <strong>Talk naturally with your agent</strong>
                <p>{live.active ? 'Your mic stays connected. Pause briefly to send your turn.' : 'Start once, speak, and hear replies. Your next turn starts automatically.'}</p>
              </div>
              <span className="live-mode-tag">Hands-free</span>
            </div>
            <div className="live-conversation-controls">
              <div className="live-conversation-status" role="status">
                <span className={`live-status-dot ${live.active ? 'is-active' : ''}`} />
                {live.state === 'off' ? 'Microphone off' : live.state === 'starting' ? 'Connecting microphone…' :
                  live.state === 'muted' ? 'Microphone muted' : live.state === 'transcribing' ? 'Transcribing your turn…' :
                  isAISpeaking ? 'Agent speaking' : isAgentResponding || live.state === 'waiting' ? 'Agent thinking…' :
                  live.state === 'recording' ? 'Hearing you · pause to send' : 'Listening · go ahead'}
              </div>
              <div className="live-buttons">
                {live.active ? (
                  <>
                    <button onClick={live.toggleMute} disabled={live.state === 'starting'}
                      aria-pressed={live.state === 'muted'}>{live.state === 'muted' ? 'Unmute' : 'Mute mic'}</button>
                    {(isAISpeaking || isAgentResponding) && <button onClick={sendInterrupt}>Stop reply</button>}
                    <button className="live-end" onClick={() => { live.stop(); sendInterrupt() }}>End conversation</button>
                  </>
                ) : (
                  <button className="live-start" disabled={micDisabled} onClick={() => {
                    void preparePlayback()
                    setTtsEnabled(true)
                    if (isAISpeaking || isAgentResponding) sendInterrupt()
                    void live.start()
                  }}>Start conversation</button>
                )}
              </div>
            </div>
            {live.active && <meter className="live-meter" aria-label="Live microphone level" min={0} max={1} value={live.level} />}
            <p className="live-hint">Listening pauses during replies. Use Stop reply to take your next turn. Switching chats or leaving this tab ends the microphone session.</p>
            {live.error && <div className="live-error" role="alert">{live.error}</div>}
          </div>
        )}
        <div style={s.inputBox}>
          <textarea
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={wsStatus === 'open' ? 'Message AI...' : 'Connecting...'}
            disabled={wsStatus !== 'open' || live.active}
            rows={2}
            style={s.textarea}
          />
          <div style={s.inputActions}>
            {isAISpeaking && (
              <button onClick={sendInterrupt} style={s.stopBtn} title="Stop">
                ◼
              </button>
            )}
            {voiceMode === 'record' && <div
              style={{
                ...s.micWrap,
                transform: `scale(${1 + speechInput.micLevel * 0.06})`,
                boxShadow: voiceInputBusy
                  ? `0 0 0 ${2 + speechInput.micLevel * 8}px rgba(45,212,191,${0.08 + speechInput.micLevel * 0.16})`
                  : 'none',
              }}
            >
              <button
                onClick={handleMicClick}
                disabled={micDisabled}
                style={{
                  ...s.micBtn,
                  ...(speechInput.state === 'listening' || speechInput.state === 'recording' ? s.micBtnListening : null),
                  ...(speechInput.state === 'error' ? s.micBtnError : null),
                  opacity: micDisabled ? 0.4 : 1,
                }}
                title={micTitle}
                aria-label={speechInput.state === 'recording' ? 'Stop recording and transcribe' : 'Record voice message'}
              >
                {speechInput.state === 'uploading' || speechInput.state === 'processing'
                  ? '…'
                  : voiceInputBusy
                    ? '⏹'
                    : speechInput.state === 'error'
                      ? '!'
                      : '🎤'}
              </button>
            </div>}
            <button
              onClick={handleSend}
              disabled={wsStatus !== 'open' || !inputText.trim() || live.active}
              style={{
                ...s.sendBtn,
                opacity: wsStatus !== 'open' || !inputText.trim() || live.active ? 0.4 : 1,
              }}
              title="Send (Enter)"
            >
              ▶
            </button>
          </div>
        </div>

        {speechInput.supported && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6, color: '#94a3b8', fontSize: 11 }}>
            <label htmlFor="microphone-device">Microphone</label>
            <select
              id="microphone-device"
              value={speechInput.microphoneId}
              onChange={(event) => speechInput.setMicrophoneId(event.target.value)}
              disabled={voiceInputBusy || live.active}
              style={{ maxWidth: 230, background: '#111827', color: '#cbd5e1', border: '1px solid #334155', borderRadius: 4 }}
            >
              <option value="">System default</option>
              {speechInput.microphones.map((device, index) => (
                <option key={device.deviceId} value={device.deviceId}>{device.label || `Microphone ${index + 1}`}</option>
              ))}
            </select>
            {speechInput.state === 'recording' && (
              <>
                <meter aria-label="Microphone input level" min={0} max={1} value={speechInput.micLevel} style={{ width: 65 }} />
                <span>{speechInput.micLevel > 0.02 ? 'Receiving audio' : 'Speak to test input'}</span>
              </>
            )}
          </div>
        )}
        {/* Emotion badge khi AI đang nói */}
        {isAISpeaking && (
          <div style={{ ...s.emotionBadge, background: EMOTION_COLORS[currentEmotion] }}>
            {currentEmotion}
          </div>
        )}
        {speechInput.state === 'error' && speechInput.errorMessage && (
          <div style={s.speechErrorBadge}>{speechInput.errorMessage}</div>
        )}
        {voiceStateLabel && speechInput.state !== 'error' && (
          <div style={s.voiceStateBadge}>
            {voiceStateLabel}
            {(speechInput.state === 'listening' || speechInput.state === 'recording') && ` · ${speechInput.remainingSeconds}s`}
          </div>
        )}
        {warmupRunning && (
          <div style={s.warmupBadge}>Backend warming up · voice may take longer</div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------
const s: Record<string, React.CSSProperties> = {
  container: {
    flex: 1,
    height: '100%',
    display: 'flex',
    flexDirection: 'column',
    background: 'rgba(0,0,0,0.42)',
    backdropFilter: 'blur(6px)',
    overflow: 'hidden',
  },
  header: {
    flex: '0 0 auto',
    display: 'flex',
    alignItems: 'center',
    padding: '10px 16px',
    borderBottom: '1px solid rgba(255,255,255,0.06)',
  },
  messageList: {
    flex: 1,
    overflowY: 'auto',
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
    padding: '20px 20px 8px',
  },
  emptyState: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 10,
    margin: 'auto',
    paddingTop: '20vh',
    opacity: 0.55,
  },
  emptyIcon: {
    fontSize: 36,
    color: '#6366f1',
  },
  emptyTitle: {
    fontSize: 20,
    fontWeight: 600,
    color: '#e2e8f0',
    fontFamily: "'JetBrains Mono', monospace",
  },
  emptySubtitle: {
    fontSize: 14,
    color: '#6b7280',
    textAlign: 'center',
    maxWidth: 320,
    lineHeight: 1.6,
  },
  messageBubble: {
    maxWidth: '72%',
    padding: '9px 13px',
    borderRadius: 12,
    color: '#e2e8f0',
    fontSize: 14,
    lineHeight: 1.55,
    backdropFilter: 'blur(6px)',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
  },
  feedbackActions: {
    display: 'flex',
    alignItems: 'center',
    gap: 4,
    justifyContent: 'flex-end',
    marginTop: 6,
  },
  feedbackBtn: {
    width: 22,
    height: 22,
    borderRadius: 6,
    border: '1px solid rgba(255,255,255,0.12)',
    background: 'rgba(15,23,42,0.45)',
    color: '#94a3b8',
    cursor: 'pointer',
    fontSize: 10,
    lineHeight: 1,
  },
  feedbackBtnActive: {
    color: '#e2e8f0',
    borderColor: 'rgba(99,102,241,0.55)',
    background: 'rgba(99,102,241,0.28)',
  },
  ttsBadge: {
    alignSelf: 'center',
    fontSize: 11,
    color: '#fbbf24',
    background: 'rgba(251,191,36,0.1)',
    border: '1px solid rgba(251,191,36,0.2)',
    borderRadius: 99,
    padding: '3px 10px',
    marginBottom: 4,
  },
  inputWrap: {
    padding: '0 16px 14px',
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
  },
  inputBox: {
    display: 'flex',
    alignItems: 'flex-end',
    gap: 8,
    background: 'rgba(15,23,42,0.75)',
    border: '1px solid rgba(255,255,255,0.1)',
    borderRadius: 14,
    padding: '8px 10px 8px 14px',
    backdropFilter: 'blur(8px)',
    minWidth: 0,
  },
  textarea: {
    flex: 1,
    resize: 'none',
    background: 'transparent',
    border: 'none',
    outline: 'none',
    color: '#e2e8f0',
    fontSize: 14,
    lineHeight: 1.5,
    fontFamily: 'inherit',
    paddingTop: 2,
    minWidth: 0,
  },
  inputActions: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    paddingBottom: 2,
    flexShrink: 0,
  },
  micWrap: {
    width: 32,
    height: 32,
    borderRadius: 8,
    transition: 'transform 0.08s linear, box-shadow 0.08s linear',
  },
  stopBtn: {
    background: 'rgba(239,68,68,0.18)',
    border: '1px solid rgba(239,68,68,0.3)',
    color: '#f87171',
    borderRadius: 8,
    width: 32,
    height: 32,
    fontSize: 11,
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
  },
  micBtn: {
    background: 'rgba(20,184,166,0.16)',
    border: '1px solid rgba(45,212,191,0.28)',
    color: '#99f6e4',
    borderRadius: 8,
    width: 32,
    height: 32,
    fontSize: 14,
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    transition: 'background 0.15s, border-color 0.15s, opacity 0.15s',
  },
  micBtnListening: {
    background: 'rgba(239,68,68,0.2)',
    borderColor: 'rgba(248,113,113,0.42)',
    color: '#fecaca',
  },
  micBtnError: {
    background: 'rgba(239,68,68,0.18)',
    borderColor: 'rgba(248,113,113,0.36)',
    color: '#fecaca',
  },
  sendBtn: {
    background: 'rgba(99,102,241,0.85)',
    border: 'none',
    color: '#fff',
    borderRadius: 8,
    width: 32,
    height: 32,
    fontSize: 13,
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    transition: 'opacity 0.15s',
  },
  emotionBadge: {
    alignSelf: 'flex-end',
    fontSize: 11,
    fontWeight: 600,
    color: '#0a0a0f',
    padding: '2px 8px',
    borderRadius: 99,
    textTransform: 'capitalize',
  },
  speechErrorBadge: {
    alignSelf: 'flex-end',
    maxWidth: '100%',
    fontSize: 11,
    color: '#fecaca',
    background: 'rgba(239,68,68,0.12)',
    border: '1px solid rgba(248,113,113,0.24)',
    borderRadius: 8,
    padding: '3px 8px',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  voiceStateBadge: {
    alignSelf: 'flex-end',
    fontSize: 11,
    color: '#99f6e4',
    background: 'rgba(20,184,166,0.1)',
    border: '1px solid rgba(45,212,191,0.18)',
    borderRadius: 8,
    padding: '3px 8px',
  },
  warmupBadge: {
    alignSelf: 'flex-end',
    fontSize: 11,
    color: '#fde68a',
    background: 'rgba(251,191,36,0.1)',
    border: '1px solid rgba(251,191,36,0.18)',
    borderRadius: 8,
    padding: '3px 8px',
  },
}
