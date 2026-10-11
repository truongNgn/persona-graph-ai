import { useCallback, useState, useRef, useEffect } from 'react'
import { useChatStore } from '../store/chatStore'
import type { LlmProvider, Project, Session } from '../types'
import { AuthPanel } from './AuthPanel'
import {
  DndContext,
  useDraggable,
  useDroppable,
  DragEndEvent,
  PointerSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core'

interface SidebarProps {
  onNewSession: () => void
  sendSetModel: (provider: LlmProvider) => void
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function formatRelative(ts: number): string {
  const diff = Date.now() - ts
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'Just now'
  if (m < 60) return `${m} min ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} hr ago`
  const d = Math.floor(h / 24)
  if (d === 1) return 'Yesterday'
  return `${d} days ago`
}

// ---------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------

function EditableLabel({
  value,
  onSave,
  onCancel,
  style,
}: {
  value: string
  onSave: (v: string) => void
  onCancel: () => void
  style?: React.CSSProperties
}) {
  const [text, setText] = useState(value)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [])

  return (
    <input
      ref={inputRef}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => onSave(text)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onSave(text)
        if (e.key === 'Escape') onCancel()
      }}
      onClick={(e) => e.stopPropagation()}
      style={{
        ...style,
        background: 'rgba(255,255,255,0.1)',
        border: '1px solid #6366f1',
        borderRadius: 4,
        color: '#fff',
        padding: '2px 4px',
        width: '100%',
        outline: 'none',
        fontSize: 'inherit',
        fontFamily: 'inherit',
      }}
    />
  )
}

function DraggableSessionItem({
  session,
  isActive,
  onDelete,
}: {
  session: Session
  isActive: boolean
  onDelete: (id: string) => void
}) {
  const { switchSession, renameSession } = useChatStore()
  const [isHover, setIsHover] = useState(false)
  const [isEditing, setIsEditing] = useState(false)

  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `session-${session.id}`,
    data: { type: 'session', sessionId: session.id },
    disabled: isEditing,
  })

  const style: React.CSSProperties = {
    ...s.sessionItem,
    background: isActive
      ? 'rgba(99,102,241,0.15)'
      : isHover
        ? 'rgba(255,255,255,0.04)'
        : 'transparent',
    borderLeft: isActive ? '2px solid #6366f1' : '2px solid transparent',
    paddingLeft: isActive ? 10 : 12,
    opacity: isDragging ? 0.4 : 1,
    transform: transform ? `translate3d(${transform.x}px, ${transform.y}px, 0)` : undefined,
    cursor: isEditing ? 'text' : 'pointer',
    zIndex: isDragging ? 100 : 1,
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      onClick={() => !isEditing && switchSession(session.id)}
      onMouseEnter={() => setIsHover(true)}
      onMouseLeave={() => setIsHover(false)}
      {...attributes}
      {...listeners}
    >
      <span style={s.sessionIcon}>○</span>
      <div style={s.sessionMeta}>
        {isEditing ? (
          <EditableLabel
            value={session.title}
            onSave={(val) => {
              renameSession(session.id, val)
              setIsEditing(false)
            }}
            onCancel={() => setIsEditing(false)}
            style={s.sessionTitle}
          />
        ) : (
          <span style={s.sessionTitle}>
            {session.title.length > 26 ? session.title.slice(0, 26) + '…' : session.title}
          </span>
        )}
        <span style={s.sessionTime}>{formatRelative(session.createdAt)}</span>
      </div>
      {isHover && !isEditing && (
        <div style={{ display: 'flex', gap: 4 }}>
          <button
            onClick={(e) => {
              e.stopPropagation()
              setIsEditing(true)
            }}
            style={s.iconBtn}
            title="Rename"
          >
            ✎
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation()
              onDelete(session.id)
            }}
            style={{ ...s.iconBtn, color: '#f87171' }}
            title="Delete"
          >
            ×
          </button>
        </div>
      )}
    </div>
  )
}

function DroppableProjectItem({
  project,
  sessions,
  activeSessionId,
  onDeleteSession,
}: {
  project: Project
  sessions: Session[]
  activeSessionId: string
  onDeleteSession: (id: string) => void
}) {
  const { renameProject, deleteProject } = useChatStore()
  const [isExpanded, setIsExpanded] = useState(true)
  const [isEditing, setIsEditing] = useState(false)
  const [isHover, setIsHover] = useState(false)

  const { isOver, setNodeRef } = useDroppable({
    id: `project-${project.id}`,
    data: { type: 'project', projectId: project.id },
  })

  return (
    <div
      ref={setNodeRef}
      onMouseEnter={() => setIsHover(true)}
      onMouseLeave={() => setIsHover(false)}
      style={{
        ...s.projectWrap,
        background: isOver ? 'rgba(99,102,241,0.08)' : 'transparent',
      }}
    >
      <div
        style={s.projectHeader}
        onClick={() => !isEditing && setIsExpanded(!isExpanded)}
      >
        <span style={{ ...s.folderIcon, transform: isExpanded ? 'rotate(90deg)' : 'none' }}>
          ▶
        </span>
        <div style={{ flex: 1, overflow: 'hidden' }}>
          {isEditing ? (
            <EditableLabel
              value={project.name}
              onSave={(val) => {
                renameProject(project.id, val)
                setIsEditing(false)
              }}
              onCancel={() => setIsEditing(false)}
              style={s.projectTitle}
            />
          ) : (
            <span style={s.projectTitle}>{project.name}</span>
          )}
        </div>
        {isHover && !isEditing && (
          <div style={{ display: 'flex', gap: 4 }}>
            <button
              onClick={(e) => {
                e.stopPropagation()
                setIsEditing(true)
              }}
              style={s.iconBtn}
            >
              ✎
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation()
                if (confirm(`Delete project "${project.name}"?`)) deleteProject(project.id)
              }}
              style={{ ...s.iconBtn, color: '#f87171' }}
            >
              ×
            </button>
          </div>
        )}
      </div>

      {isExpanded && (
        <div style={s.projectContent}>
          {sessions.length === 0 && (
            <div style={s.emptyProjectHint}>Drop sessions here</div>
          )}
          {sessions.map((session) => (
            <DraggableSessionItem
              key={session.id}
              session={session}
              isActive={session.id === activeSessionId}
              onDelete={onDeleteSession}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------
export function Sidebar({ onNewSession, sendSetModel }: SidebarProps) {
  const {
    sessions,
    projects,
    activeSessionId,
    wsStatus,
    llmProvider,
    modelsCatalog,
    ttsEnabled,
    routerEnabled,
    autoSendVoiceTranscript,
    deleteSession,
    setTtsEnabled,
    setRouterEnabled,
    setAutoSendVoiceTranscript,
    setLlmProvider,
    createProject,
    moveSessionToProject,
  } = useChatStore()

  const [confirmId, setConfirmId] = useState<string | null>(null)

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: 5 },
    })
  )

  const handleProviderChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      const p = e.target.value as LlmProvider
      setLlmProvider(p)
      sendSetModel(p)
    },
    [sendSetModel, setLlmProvider]
  )

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event

    if (over && active.data.current?.type === 'session' && over.data.current?.type === 'project') {
      const sessionId = active.data.current.sessionId
      const projectId = over.data.current.projectId
      moveSessionToProject(sessionId, projectId)
    } else if (!over && active.data.current?.type === 'session') {
      // If dropped outside, maybe move back to recent?
      // Actually, if we drop it on the "Recent" area, we should handle that.
    }
  }

  const handleDeleteSession = useCallback((id: string) => {
    setConfirmId(id)
  }, [])

  const handleConfirmDelete = useCallback(
    (e: React.MouseEvent, id: string) => {
      e.stopPropagation()
      deleteSession(id)
      setConfirmId(null)
    },
    [deleteSession]
  )

  const handleCancelDelete = useCallback((e: React.MouseEvent) => {
    e.stopPropagation()
    setConfirmId(null)
  }, [])

  const handleCreateProject = () => {
    const name = prompt('New project name:')
    if (name) createProject(name)
  }

  const statusColor = { connecting: '#fbbf24', open: '#34d399', closed: '#6b7280', error: '#f87171' }[wsStatus]

  // Filter sessions
  const unassignedSessions = sessions
    .filter((s) => !s.projectId)
    .sort((a, b) => b.createdAt - a.createdAt)

  return (
    <div style={s.sidebar}>
      {/* ── Header ─────────────────────────────────────── */}
      <div style={s.header}>
        <span style={s.logo}>◈</span>
        <span style={s.title}>AI Chatbot</span>
      </div>

      {/* ── New Chat button ─────────────────────────────── */}
      <div style={s.newChatWrap}>
        <NewChatButton onClick={onNewSession} />
      </div>

      <div style={s.divider} />

      {/* ── Drag & Drop Area ────────────────────────────── */}
      <div style={s.sessionList}>
        <DndContext
          sensors={sensors}
          onDragEnd={handleDragEnd}
        >
          {/* Projects Section */}
          <div style={s.sectionHeader}>
            <span style={s.sectionLabel}>Projects</span>
            <button onClick={handleCreateProject} style={s.addProjectBtn}>+</button>
          </div>

          {projects.map((project) => (
            <DroppableProjectItem
              key={project.id}
              project={project}
              sessions={sessions.filter((s) => s.projectId === project.id)}
              activeSessionId={activeSessionId}
              onDeleteSession={handleDeleteSession}
            />
          ))}

          <div style={s.divider} />

          {/* Recent Section (also droppable to remove from project) */}
          <RecentDroppableArea
            sessions={unassignedSessions}
            activeSessionId={activeSessionId}
            onDeleteSession={handleDeleteSession}
            confirmId={confirmId}
            onConfirmDelete={handleConfirmDelete}
            onCancelDelete={handleCancelDelete}
          />
        </DndContext>
      </div>

      {/* ── Footer ─────────────────────────────────────── */}
      <div style={s.footer}>
        <div style={s.divider} />
        <AuthPanel />
        <div style={s.footerRow}>
          <span style={s.footerLabel}>{ttsEnabled ? '🔊' : '🔇'} Voice</span>
          <Toggle checked={ttsEnabled} onChange={setTtsEnabled} />
        </div>
        <div style={s.footerRow}>
          <span style={s.footerLabel}>⚡ Auto Model</span>
          <RouterToggle checked={routerEnabled} onChange={setRouterEnabled} />
        </div>
        <div style={s.footerRow}>
          <span style={s.footerLabel}>🎙 Auto Send</span>
          <Toggle checked={autoSendVoiceTranscript} onChange={setAutoSendVoiceTranscript} />
        </div>
        <div style={s.footerRow}>
          <span style={s.footerLabel}>Model</span>
          <select value={llmProvider} onChange={handleProviderChange} disabled={wsStatus !== 'open'} style={s.select}>
            {modelsCatalog ? (
              modelsCatalog.groups.map((group) => (
                <optgroup key={group.label} label={group.label}>
                  {group.models.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.display_name}
                    </option>
                  ))}
                </optgroup>
              ))
            ) : (
              <>
                <option value="ollama">Llama 3 (Local)</option>
                <option value="qwen">Qwen 1.5B (Local)</option>
                <option value="deepseek">DeepSeek v3</option>
                <option value="gemini">Google Gemini</option>
              </>
            )}
          </select>
        </div>
        <div style={s.footerRow}>
          <span style={{ ...s.statusDot, background: statusColor }} />
          <span style={s.statusText}>{wsStatus === 'open' ? 'Connected' : wsStatus === 'connecting' ? 'Connecting…' : wsStatus}</span>
        </div>
      </div>
    </div>
  )
}

function RecentDroppableArea({
  sessions,
  activeSessionId,
  onDeleteSession,
  confirmId,
  onConfirmDelete,
  onCancelDelete,
}: {
  sessions: Session[]
  activeSessionId: string
  onDeleteSession: (id: string) => void
  confirmId: string | null
  onConfirmDelete: (e: React.MouseEvent, id: string) => void
  onCancelDelete: (e: React.MouseEvent) => void
}) {
  const { isOver, setNodeRef } = useDroppable({
    id: 'recent-area',
    data: { type: 'project', projectId: null }, // Moving to null unassigns
  })

  return (
    <div
      ref={setNodeRef}
      style={{
        ...s.recentArea,
        background: isOver ? 'rgba(255,255,255,0.03)' : 'transparent',
      }}
    >
      <div style={s.sectionLabel}>Recent</div>
      {sessions.length === 0 && <div style={s.emptyHint}>No conversations yet</div>}
      {sessions.map((session) => {
        if (confirmId === session.id) {
          return (
            <div key={session.id} style={{ ...s.sessionItem, background: 'rgba(239,68,68,0.08)' }}>
              <span style={{ fontSize: 12, color: '#f87171', flex: 1 }}>Delete?</span>
              <button onClick={(e) => onConfirmDelete(e, session.id)} style={s.confirmBtn}>Delete</button>
              <button onClick={onCancelDelete} style={s.cancelBtn}>Cancel</button>
            </div>
          )
        }
        return (
          <DraggableSessionItem
            key={session.id}
            session={session}
            isActive={session.id === activeSessionId}
            onDelete={onDeleteSession}
          />
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Basic Components (Toggles, buttons)
// ---------------------------------------------------------------------------
function Toggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button onClick={() => onChange(!checked)} style={{ ...s.toggleBase, background: checked ? '#6366f1' : 'rgba(255,255,255,0.12)' }}>
      <span style={{ ...s.toggleThumb, left: checked ? 19 : 3 }} />
    </button>
  )
}
function RouterToggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button onClick={() => onChange(!checked)} style={{ ...s.toggleBase, background: checked ? '#10b981' : 'rgba(255,255,255,0.12)' }}>
      <span style={{ ...s.toggleThumb, left: checked ? 19 : 3 }} />
    </button>
  )
}
function NewChatButton({ onClick }: { onClick: () => void }) {
  const [hover, setHover] = useState(false)
  return (
    <button
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        ...s.newChatBtn,
        background: hover ? 'rgba(99,102,241,0.18)' : 'rgba(255,255,255,0.04)',
        borderColor: hover ? 'rgba(99,102,241,0.5)' : 'rgba(255,255,255,0.1)',
      }}
    >
      <span style={{ fontSize: 14 }}>+</span>
      <span>New Chat</span>
    </button>
  )
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------
const s: Record<string, React.CSSProperties> = {
  sidebar: {
    width: 240, minWidth: 240, height: '100%', display: 'flex', flexDirection: 'column',
    background: '#151521', borderRight: '1px solid rgba(255,255,255,0.07)',
    fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
    userSelect: 'none', overflow: 'hidden',
  },
  header: { display: 'flex', alignItems: 'center', gap: 8, padding: '16px 14px 12px' },
  logo: { fontSize: 18, color: '#6366f1' },
  title: { fontSize: 14, fontWeight: 600, color: '#e2e8f0' },
  newChatWrap: { padding: '0 10px 8px' },
  newChatBtn: {
    width: '100%', display: 'flex', alignItems: 'center', gap: 6, padding: '7px 12px',
    border: '1px solid', borderRadius: 8, color: '#c4c9d4', fontSize: 13, cursor: 'pointer',
    transition: 'background 0.15s, border-color 0.15s', fontFamily: 'inherit',
  },
  divider: { height: 1, background: 'rgba(255,255,255,0.06)', margin: '4px 0' },
  sectionHeader: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', paddingRight: 10 },
  sectionLabel: {
    fontSize: 10, fontWeight: 600, color: '#4b5563', letterSpacing: '0.1em',
    textTransform: 'uppercase', padding: '8px 14px 4px',
  },
  addProjectBtn: {
    background: 'none', border: 'none', color: '#4b5563', cursor: 'pointer', fontSize: 16,
    padding: '4px 8px', borderRadius: 4, transition: 'color 0.2s',
  },
  sessionList: { flex: 1, overflowY: 'auto', paddingBottom: 4 },
  emptyHint: { fontSize: 12, color: '#374151', padding: '10px 14px' },
  recentArea: { minHeight: 60, transition: 'background 0.2s' },
  
  projectWrap: { marginBottom: 2, transition: 'background 0.2s' },
  projectHeader: {
    display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px',
    cursor: 'pointer', transition: 'background 0.1s', color: '#9ca3af',
  },
  projectTitle: { fontSize: 12, fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
  projectContent: { paddingLeft: 12, borderLeft: '1px solid rgba(255,255,255,0.03)', marginLeft: 14 },
  folderIcon: { fontSize: 8, transition: 'transform 0.2s', color: '#4b5563' },
  emptyProjectHint: { fontSize: 10, color: '#374151', padding: '4px 8px', fontStyle: 'italic' },

  sessionItem: {
    display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px',
    cursor: 'pointer', borderRadius: 0, transition: 'background 0.1s', minHeight: 40,
  },
  sessionIcon: { fontSize: 8, color: '#4b5563', flexShrink: 0, marginTop: 1 },
  sessionMeta: { flex: 1, display: 'flex', flexDirection: 'column', gap: 0, overflow: 'hidden' },
  sessionTitle: { fontSize: 12, color: '#d1d5db', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
  sessionTime: { fontSize: 9, color: '#3f4652' },
  
  iconBtn: {
    background: 'rgba(255,255,255,0.05)', border: 'none', color: '#6b7280',
    borderRadius: 4, width: 18, height: 18, fontSize: 12, cursor: 'pointer',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
  },
  confirmBtn: {
    background: 'rgba(239,68,68,0.2)', border: '1px solid rgba(239,68,68,0.35)',
    color: '#f87171', borderRadius: 4, padding: '2px 6px', fontSize: 11, cursor: 'pointer',
  },
  cancelBtn: {
    background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)',
    color: '#6b7280', borderRadius: 4, padding: '2px 6px', fontSize: 11, cursor: 'pointer',
  },
  toggleBase: {
    position: 'relative', width: 36, height: 20, borderRadius: 10, border: 'none',
    cursor: 'pointer', padding: 0, transition: 'background 0.2s', flexShrink: 0,
  },
  toggleThumb: {
    position: 'absolute', top: 3, width: 14, height: 14, borderRadius: '50%',
    background: '#fff', transition: 'left 0.2s',
  },
  footer: { paddingBottom: 12 },
  footerRow: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 14px', gap: 8 },
  footerLabel: { fontSize: 12, color: '#6b7280', flex: 1 },
  select: {
    background: 'rgba(255,255,255,0.05)', color: '#9ca3af', border: '1px solid rgba(255,255,255,0.08)',
    borderRadius: 5, padding: '3px 6px', fontSize: 11, cursor: 'pointer', outline: 'none', maxWidth: 100,
  },
  statusDot: { width: 7, height: 7, borderRadius: '50%', flexShrink: 0 },
  statusText: { fontSize: 11, color: '#6b7280', flex: 1 },
}
