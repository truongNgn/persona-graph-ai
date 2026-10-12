import { useCallback, useEffect } from 'react'
import { ChatInterface } from './components/ChatInterface'
import { Sidebar } from './components/Sidebar'
import { RightSidebar } from './components/RightSidebar'
import { LoginScreen } from './components/LoginScreen'
import { useWebSocket } from './hooks/useWebSocket'
import { useAudioQueue } from './hooks/useAudioQueue'
import { useChatStore } from './store/chatStore'
import { AdminDashboard } from './components/AdminDashboard'

export function App() {
  const authUser = useChatStore((state) => state.authUser)
  if (!authUser) return <LoginScreen />
  if (window.location.pathname === '/admin/monitoring') return <AdminDashboard />
  return <AuthenticatedApp />
}

function AuthenticatedApp() {
  const { stopPlayback, preparePlayback } = useAudioQueue()
  const { sendMessage, sendInterrupt, sendSetModel } = useWebSocket(stopPlayback)
  const authToken = useChatStore((state) => state.authToken)
  const refreshServerConversations = useChatStore((state) => state.refreshServerConversations)

  const handleInterrupt = useCallback(() => {
    stopPlayback()
    sendInterrupt()
  }, [stopPlayback, sendInterrupt])

  useEffect(() => {
    if (authToken) void refreshServerConversations()
  }, [authToken, refreshServerConversations])

  useEffect(() => {
    void useChatStore.getState().fetchCharacters()
    void useChatStore.getState().fetchModelsCatalog()
  }, [])

  // New session: update store + reconnect WS với sessionId mới không cần thiết
  // vì useWebSocket luôn đọc activeSessionId từ store khi gửi message.
  const handleNewSession = useCallback(() => {
    useChatStore.getState().createNewSession()
  }, [])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', display: 'flex' }}>
      <Sidebar onNewSession={handleNewSession} sendSetModel={sendSetModel} />
      <div style={{ flex: 1, position: 'relative' }}>
        <ChatInterface
          sendMessage={sendMessage}
          sendInterrupt={handleInterrupt}
          preparePlayback={preparePlayback}
        />
      </div>
      <RightSidebar />
    </div>
  )
}
