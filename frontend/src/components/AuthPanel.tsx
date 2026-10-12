import type { CSSProperties } from 'react'
import { useEffect, useState } from 'react'
import { useChatStore } from '../store/chatStore'
import { API_BASE_URL } from '../config/backend'

// Rendered only inside the authenticated app (see App.tsx / LoginScreen.tsx
// for the pre-login flow), so authUser is always present here.
export function AuthPanel() {
  const { authUser, authToken, logout, refreshServerConversations } = useChatStore()
  const [admin, setAdmin] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    setAdmin(false)
    if (authToken) void fetch(`${API_BASE_URL}/api/admin/monitoring/access`, {
      headers: { Authorization: `Bearer ${authToken}` }, signal: controller.signal,
    }).then(async response => response.ok ? response.json() as Promise<{ allowed: boolean }> : { allowed: false })
      .then(result => setAdmin(result.allowed)).catch(() => {})
    return () => controller.abort()
  }, [authToken])

  if (!authUser) return null

  return (
    <div style={s.panel}>
      <div style={s.userLine}>
        <span style={s.userDot} />
        <span style={s.userText}>{authUser.display_name || authUser.email || 'Signed in'}</span>
      </div>
      <div style={s.actions}>
        {admin && <a href="/admin/monitoring" style={{ ...s.secondaryBtn, textDecoration: 'none' }}>Admin</a>}
        <button onClick={() => void refreshServerConversations()} style={s.secondaryBtn}>
          Sync
        </button>
        <button onClick={logout} style={s.secondaryBtn}>
          Logout
        </button>
      </div>
    </div>
  )
}

const s: Record<string, CSSProperties> = {
  panel: {
    padding: '8px 10px',
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
  },
  secondaryBtn: {
    border: '1px solid rgba(255,255,255,0.08)',
    borderRadius: 6,
    padding: '5px 7px',
    background: 'rgba(255,255,255,0.04)',
    color: '#9ca3af',
    fontSize: 11,
    cursor: 'pointer',
  },
  userLine: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    minWidth: 0,
  },
  userDot: {
    width: 7,
    height: 7,
    borderRadius: '50%',
    background: '#34d399',
    flexShrink: 0,
  },
  userText: {
    color: '#d1d5db',
    fontSize: 11,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
  actions: {
    display: 'flex',
    gap: 6,
  },
}
