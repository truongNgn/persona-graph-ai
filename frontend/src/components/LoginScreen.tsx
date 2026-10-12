import { useState, useEffect } from 'react'
import type { CSSProperties } from 'react'
import { useChatStore } from '../store/chatStore'
import { API_BASE_URL } from '../config/backend'

export function LoginScreen() {
  const { authLoading, authError, login, register, loginGoogle, devLogin } = useChatStore()
  const [devLoginStatus, setDevLoginStatus] = useState<'checking' | 'enabled' | 'disabled' | 'unavailable'>('checking')
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')

  const googleClientId = import.meta.env.VITE_GOOGLE_CLIENT_ID || ''

  useEffect(() => {
    const controller = new AbortController()
    let retry: ReturnType<typeof setTimeout> | undefined
    const check = async () => {
      try {
        const response = await fetch(`${API_BASE_URL}/api/auth/dev-status`, { signal: controller.signal })
        if (!response.ok) throw new Error('Backend unavailable')
        const status = await response.json() as { enabled: boolean }
        if (!controller.signal.aborted) setDevLoginStatus(status.enabled === true ? 'enabled' : 'disabled')
      } catch {
        if (controller.signal.aborted) return
        setDevLoginStatus('unavailable')
        retry = setTimeout(() => void check(), 3000)
      }
    }
    void check()
    return () => { controller.abort(); if (retry) clearTimeout(retry) }
  }, [])

  useEffect(() => {
    if (!googleClientId) return

    let mounted = true
    const initGoogle = () => {
      if (!mounted) return
      const g = (window as any).google
      if (g?.accounts?.id) {
        g.accounts.id.initialize({
          client_id: googleClientId,
          callback: async (res: any) => {
            if (res.credential) {
              await loginGoogle(res.credential)
            }
          },
        })
        const btnEl = document.getElementById('google-signin-btn')
        if (btnEl) {
          g.accounts.id.renderButton(btnEl, {
            theme: 'outline',
            size: 'large',
            width: 288,
          })
        }
      } else {
        setTimeout(initGoogle, 200)
      }
    }

    initGoogle()
    return () => {
      mounted = false
    }
  }, [googleClientId, loginGoogle])

  const submit = async () => {
    if (!email.trim() || !password) return
    mode === 'login'
      ? await login(email.trim(), password)
      : await register(email.trim(), password, displayName.trim() || undefined)
  }

  return (
    <div style={s.wrap}>
      <div style={s.card}>
        <h1 style={s.title}>Interactive AI Chatbot</h1>
        <p style={s.subtitle}>Sign in to start chatting with your AI agent</p>

        {(devLoginStatus === 'enabled' || (import.meta.env.DEV && devLoginStatus !== 'disabled')) && (
          <>
            <button type="button" disabled={authLoading || devLoginStatus !== 'enabled'}
              onClick={() => void devLogin()} style={{ ...s.devBtn, opacity: devLoginStatus === 'enabled' ? 1 : 0.5 }}>
              {authLoading ? 'Working...' : 'Quick login as Dev Tester'}
            </button>
            {devLoginStatus !== 'enabled' && (
              <div role="status" style={s.devStatus}>
                {devLoginStatus === 'checking' ? 'Checking developer login…' : 'Waiting for the local backend. Developer login will enable automatically when it connects.'}
              </div>
            )}
          </>
        )}

        <div style={s.tabs}>
          <button
            onClick={() => setMode('login')}
            style={{ ...s.tab, ...(mode === 'login' ? s.tabActive : null) }}
          >
            Login
          </button>
          <button
            onClick={() => setMode('register')}
            style={{ ...s.tab, ...(mode === 'register' ? s.tabActive : null) }}
          >
            Register
          </button>
        </div>

        {mode === 'register' && (
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="Display name"
            style={s.input}
          />
        )}
        <input
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="Email"
          style={s.input}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void submit()
          }}
        />
        <input
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          type="password"
          style={s.input}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void submit()
          }}
        />

        {authError && <div style={s.error}>{authError.slice(0, 200)}</div>}

        <button disabled={authLoading} onClick={() => void submit()} style={s.primaryBtn}>
          {authLoading ? 'Working...' : mode === 'login' ? 'Login' : 'Create account'}
        </button>

        <div style={s.divider}>
          <div style={s.dividerLine}></div>
          <span style={s.dividerText}>or</span>
          <div style={s.dividerLine}></div>
        </div>

        {googleClientId ? (
          <div style={s.googleWrapper}>
            <div id="google-signin-btn"></div>
          </div>
        ) : (
          <div style={s.googleWarning}>
            Google Login requires VITE_GOOGLE_CLIENT_ID in .env.local
          </div>
        )}
      </div>
    </div>
  )
}

const s: Record<string, CSSProperties> = {
  devStatus: { fontSize: 11, color: '#94a3b8', lineHeight: 1.5 },
  devBtn: {
    border: '1px solid rgba(52,211,153,0.45)',
    borderRadius: 8,
    padding: '10px 12px',
    background: 'rgba(52,211,153,0.12)',
    color: '#6ee7b7',
    fontSize: 13,
    fontWeight: 600,
    cursor: 'pointer',
    marginBottom: 4,
  },
  wrap: {
    width: '100%',
    height: '100%',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    background: 'radial-gradient(circle at top, #1e1b3a 0%, #0b0f19 70%)',
  },
  card: {
    width: 340,
    display: 'flex',
    flexDirection: 'column',
    gap: 10,
    padding: '28px 26px',
    borderRadius: 14,
    border: '1px solid rgba(255,255,255,0.08)',
    background: 'rgba(15,23,42,0.75)',
    boxShadow: '0 20px 60px rgba(0,0,0,0.45)',
  },
  title: {
    margin: 0,
    fontSize: 18,
    color: '#f1f5f9',
    textAlign: 'center',
  },
  subtitle: {
    margin: '0 0 8px',
    fontSize: 12,
    color: '#94a3b8',
    textAlign: 'center',
  },
  tabs: {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: 6,
    marginBottom: 4,
  },
  tab: {
    border: '1px solid rgba(255,255,255,0.08)',
    background: 'rgba(255,255,255,0.04)',
    color: '#6b7280',
    borderRadius: 8,
    padding: '8px 10px',
    fontSize: 13,
    cursor: 'pointer',
  },
  tabActive: {
    color: '#e2e8f0',
    borderColor: 'rgba(99,102,241,0.45)',
    background: 'rgba(99,102,241,0.18)',
  },
  input: {
    width: '100%',
    boxSizing: 'border-box',
    border: '1px solid rgba(255,255,255,0.08)',
    background: 'rgba(15,23,42,0.5)',
    color: '#e2e8f0',
    borderRadius: 8,
    padding: '10px 12px',
    fontSize: 13,
    outline: 'none',
  },
  primaryBtn: {
    border: 'none',
    borderRadius: 8,
    padding: '10px 12px',
    background: '#6366f1',
    color: '#fff',
    fontSize: 13,
    fontWeight: 600,
    cursor: 'pointer',
    marginTop: 4,
  },
  error: {
    color: '#fecaca',
    fontSize: 11,
    lineHeight: 1.4,
  },
  divider: {
    display: 'flex',
    alignItems: 'center',
    textAlign: 'center',
    margin: '14px 0 8px',
  },
  dividerLine: {
    flex: 1,
    height: 1,
    background: 'rgba(255,255,255,0.08)',
  },
  dividerText: {
    padding: '0 10px',
    color: '#6b7280',
    fontSize: 12,
  },
  googleWrapper: {
    display: 'flex',
    justifyContent: 'center',
    width: '100%',
    minHeight: 40,
    marginTop: 4,
  },
  googleWarning: {
    color: '#94a3b8',
    fontSize: 11,
    textAlign: 'center',
    padding: '8px',
    borderRadius: 8,
    border: '1px dashed rgba(255,255,255,0.1)',
    background: 'rgba(255,255,255,0.02)',
    marginTop: 4,
  },
}
