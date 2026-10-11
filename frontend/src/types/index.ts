// Mirrors backend app/models.py

export type Emotion = 'joy' | 'sad' | 'neutral' | 'thinking' | 'surprise' | 'anger'

// Server → Client
export interface AudioChunkPayload {
  type: 'audio_chunk'
  text: string
  emotion: Emotion
  audio_base64: string
  duration_ms: number
  visemes: VisemeKeyframe[]
  turn_id?: string | null
}

export interface VisemeKeyframe {
  start: number  // seconds from audio start
  end: number    // seconds from audio start
  value: string  // Rhubarb phoneme: "A" | "B" | "C" | "D" | "E" | "F" | "G" | "H" | "X"
}

export interface DonePayload {
  type: 'done'
}

export interface ErrorPayload {
  type: 'error'
  message: string
}

export interface ClearQueuePayload {
  type: 'clear_queue'
}

export type ServerMessage =
  | AudioChunkPayload
  | DonePayload
  | ErrorPayload
  | ClearQueuePayload
  | ModelChangedPayload
  | ConnectedPayload

// Session management
export interface Session {
  id: string           // UUID
  title: string        // ~8 từ đầu của user message đầu tiên
  createdAt: number    // Date.now() timestamp
  messages: ChatMessage[]
  projectId?: string   // ID của project chứa session này
}

export interface Project {
  id: string
  name: string
  createdAt: number
}

// Character roleplay — mirrors backend app/character_registry.py::Character.public_dict()
export interface Character {
  id: string
  display_name: string
  voice: string | null
  avatar: string | null           // resolved URL (local static or GCS), not a raw key
  avatar_thumbnail: string | null // resolved URL, same as avatar
  description: string
}

export interface CharactersResponse {
  characters: Character[]
  default: string
}

// Client → Server
export interface UserMessagePayload {
  type: 'user_message'
  text: string
  user_id?: string
  session_id?: string
  character_id?: string
  tts_enabled?: boolean
  router_enabled?: boolean
  voice?: string
}

export interface InterruptPayload {
  type: 'interrupt'
}

// Chat history
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  emotion?: Emotion
  turnId?: string
  feedbackRating?: 'up' | 'down'
}

export type WsStatus = 'connecting' | 'open' | 'closed' | 'error'

export type LlmProvider = string

export interface SetModelPayload {
  type: 'set_model'
  provider: LlmProvider
}

export interface ModelChangedPayload {
  type: 'model_changed'
  provider: LlmProvider
}

export interface ConnectedPayload {
  type: 'connected'
  provider: LlmProvider
  warmup?: WarmupStatus
}

export interface SttStatus {
  enabled: boolean
  provider: string
  language: string | null
  max_file_mb: number
  max_duration_seconds: number
}

export interface TranscribeResult {
  text: string
  language: string | null
  confidence: number | null
}

export interface WarmupStatus {
  status: 'idle' | 'running' | 'ready' | 'degraded' | 'disabled' | 'cancelled'
  started_at: number | null
  finished_at: number | null
  duration_ms: number | null
  warmed: string[]
  failed: Record<string, string>
}

export interface AuthUser {
  id: string
  email: string
  display_name?: string | null
}

export interface AuthResponse {
  access_token: string
  token_type: 'bearer'
  user: AuthUser
}

export interface ConversationSummary {
  id: string
  title: string | null
  character_id: string
  created_at: number
  updated_at: number
}

export interface ConversationMessage {
  id: number
  role: 'human' | 'ai'
  content: string
  emotion?: Emotion | null
  turn_id?: string | null
  created_at: number
}

export interface ConversationDetail {
  conversation: ConversationSummary
  messages: ConversationMessage[]
}

export interface ModelInfo {
  id: string
  display_name: string
  tier: string
}

export interface ModelGroup {
  label: string
  models: ModelInfo[]
}

export interface ModelsCatalog {
  default: string
  groups: ModelGroup[]
}
