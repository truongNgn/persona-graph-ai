// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useLiveConversation } from './useLiveConversation'
import { useChatStore } from '../store/chatStore'

let amplitude = 0
let track: { enabled: boolean; stop: ReturnType<typeof vi.fn>; onended: (() => void) | null }
let root: Root
let live: ReturnType<typeof useLiveConversation>
let transcript: ReturnType<typeof vi.fn>
let fetchMock: ReturnType<typeof vi.fn>
let getMedia: ReturnType<typeof vi.fn>

class FakeRecorder {
  static recordings = 0
  state = 'inactive'
  mimeType = 'audio/webm'
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  onerror: (() => void) | null = null
  start() { this.state = 'recording'; FakeRecorder.recordings++ }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['speech']) })
    this.onstop?.()
  }
}

function Harness() {
  live = useLiveConversation(transcript, '')
  return null
}

async function advance(ms: number, volume = 0) {
  amplitude = volume
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

beforeEach(async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  track = { enabled: true, stop: vi.fn(), onended: null }
  getMedia = vi.fn().mockResolvedValue({ getTracks: () => [track], getAudioTracks: () => [track] })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: getMedia } })
  vi.stubGlobal('AudioContext', class {
    resume = vi.fn().mockResolvedValue(undefined)
    close = vi.fn().mockResolvedValue(undefined)
    createAnalyser() { return { fftSize: 1024, getFloatTimeDomainData: (values: Float32Array) => values.fill(amplitude) } }
    createMediaStreamSource() { return { connect: vi.fn() } }
  })
  vi.stubGlobal('MediaRecorder', FakeRecorder)
  amplitude = 0
  FakeRecorder.recordings = 0
  fetchMock = vi.fn().mockImplementation((url: string) => Promise.resolve({
    ok: true, json: async () => url.endsWith('/status') ? { enabled: true } : { text: 'Hello agent' },
  }))
  vi.stubGlobal('fetch', fetchMock)
  transcript = vi.fn(() => { useChatStore.setState({ isAgentResponding: true }); return true })
  useChatStore.setState({ wsStatus: 'open', ttsEnabled: true, isAgentResponding: false, isAISpeaking: false, audioQueue: [] })
  root = createRoot(document.createElement('div'))
  await act(async () => root.render(<Harness />))
})

afterEach(async () => {
  await act(async () => root.unmount())
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('hands-free conversation', () => {
  it('sends after silence, suppresses capture throughout agent generation/playback, then accepts the next turn', async () => {
    await act(async () => live.start())
    await advance(2000)
    expect(FakeRecorder.recordings).toBe(0)
    await advance(400, .04)
    await advance(1100)
    expect(transcript).toHaveBeenCalledWith('Hello agent')
    await advance(2000, .04)
    expect(FakeRecorder.recordings).toBe(1)
    await act(async () => useChatStore.setState({ isAgentResponding: false, isAISpeaking: true }))
    await advance(1000, .04)
    expect(FakeRecorder.recordings).toBe(1)
    await act(async () => useChatStore.setState({ isAISpeaking: false }))
    await advance(600)
    await advance(400, .04)
    await advance(1100)
    expect(transcript).toHaveBeenCalledTimes(2)
  })

  it('ignores brief noise and resumes listening', async () => {
    await act(async () => live.start())
    await advance(50, .04)
    await advance(1100)
    expect(transcript).not.toHaveBeenCalled()
    await advance(400, .04)
    await advance(1100)
    expect(transcript).toHaveBeenCalledTimes(1)
  })

  it('discards an unfinished turn on mute and resumes on unmute', async () => {
    await act(async () => live.start())
    await advance(400, .04)
    await act(async () => live.toggleMute())
    expect(track.enabled).toBe(false)
    await advance(1100)
    expect(transcript).not.toHaveBeenCalled()
    await act(async () => live.toggleMute())
    await advance(600)
    await advance(400, .04)
    await advance(1100)
    expect(transcript).toHaveBeenCalledTimes(1)
  })

  it('discards late transcription and releases capture when ended', async () => {
    let resolve!: (value: unknown) => void
    fetchMock.mockImplementation((url: string) => url.endsWith('/status')
      ? Promise.resolve({ ok: true, json: async () => ({ enabled: true }) })
      : new Promise((done) => { resolve = done }))
    await act(async () => live.start())
    await advance(400, .04)
    await advance(1100)
    await act(async () => live.stop())
    await act(async () => resolve({ ok: true, json: async () => ({ text: 'Stale turn' }) }))
    expect(transcript).not.toHaveBeenCalled()
    expect(track.stop).toHaveBeenCalled()
    expect(live.state).toBe('off')
  })

  it('releases microphone permission resolved after cancellation', async () => {
    let resolve!: (value: unknown) => void
    getMedia.mockImplementation(() => new Promise((done) => { resolve = done }))
    let pending!: Promise<void>
    await act(async () => { pending = live.start(); await Promise.resolve() })
    await act(async () => live.stop())
    await act(async () => {
      resolve({ getTracks: () => [track], getAudioTracks: () => [track] })
      await pending
    })
    expect(track.stop).toHaveBeenCalled()
    expect(live.state).toBe('off')
  })

  it('ends capture on chat switch and disconnect', async () => {
    await act(async () => live.start())
    await act(async () => useChatStore.setState({ activeSessionId: 'another-chat' }))
    expect(track.stop).toHaveBeenCalled()
    expect(live.state).toBe('off')
    await act(async () => live.start())
    await act(async () => useChatStore.setState({ wsStatus: 'closed' }))
    expect(live.state).toBe('off')
  })

  it('shows transcription failures and stops capture so typed chat remains usable', async () => {
    fetchMock.mockImplementation((url: string) => Promise.resolve({
      ok: url.endsWith('/status'),
      json: async () => url.endsWith('/status') ? { enabled: true } : { detail: 'Speech service unavailable' },
    }))
    await act(async () => live.start())
    await advance(400, .04)
    await advance(1100)
    expect(live.error).toBe('Speech service unavailable')
    expect(live.state).toBe('off')
    expect(track.stop).toHaveBeenCalled()
  })

  it('bounds long utterances and waits until queued reply audio has drained', async () => {
    await act(async () => live.start())
    await advance(60000, .04)
    expect(transcript).toHaveBeenCalledTimes(1)
    await act(async () => useChatStore.setState({ isAgentResponding: false, audioQueue: [{} as never] }))
    await advance(1000, .04)
    expect(FakeRecorder.recordings).toBe(1)
    await act(async () => useChatStore.setState({ audioQueue: [] }))
    await advance(600)
    await advance(400, .04)
    expect(FakeRecorder.recordings).toBe(2)
  })

  it('ends when the tab is hidden and releases capture on unmount', async () => {
    await act(async () => live.start())
    Object.defineProperty(document, 'hidden', { configurable: true, value: true })
    await act(async () => document.dispatchEvent(new Event('visibilitychange')))
    expect(live.state).toBe('off')
    expect(track.stop).toHaveBeenCalled()
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
    await act(async () => live.start())
    const stops = track.stop.mock.calls.length
    await act(async () => root.unmount())
    expect(track.stop.mock.calls.length).toBeGreaterThan(stops)
  })
})
