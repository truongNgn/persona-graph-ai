let rawHost = import.meta.env.VITE_BACKEND_HOST || 'localhost'
rawHost = rawHost.replace(/^https?:\/\//i, '').replace(/\/+$/, '')

const BACKEND_HOST = rawHost
const BACKEND_PORT = import.meta.env.VITE_BACKEND_PORT || '8001'

const isLocalhost = BACKEND_HOST === 'localhost' || BACKEND_HOST === '127.0.0.1'
const useSameOrigin = import.meta.env.PROD && !import.meta.env.VITE_BACKEND_HOST

export const API_BASE_URL = useSameOrigin ? window.location.origin : isLocalhost
  ? `http://${BACKEND_HOST}:${BACKEND_PORT}`
  : `https://${BACKEND_HOST}`

export const WS_BASE_URL = useSameOrigin ? window.location.origin.replace(/^http/, 'ws') : isLocalhost
  ? `ws://${BACKEND_HOST}:${BACKEND_PORT}`
  : `wss://${BACKEND_HOST}`

