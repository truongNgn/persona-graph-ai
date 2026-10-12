// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { useAudioQueue } from './useAudioQueue'
import { useChatStore } from '../store/chatStore'

vi.mock('./useLipSync', () => ({ startLipSync: vi.fn(), startAmplitudeLipSync: vi.fn(), stopLipSync: vi.fn() }))

afterEach(() => vi.unstubAllGlobals())

it('does not play an interrupted reply when audio decoding finishes late', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  let resolveDecode!: (buffer: AudioBuffer) => void
  const decode = new Promise<AudioBuffer>((resolve) => { resolveDecode = resolve })
  const createSource = vi.fn()
  vi.stubGlobal('AudioContext', class {
    state = 'running'
    close = vi.fn().mockResolvedValue(undefined)
    decodeAudioData = vi.fn(() => decode)
    createBufferSource = createSource
  })
  useChatStore.setState({ audioQueue: [], isAISpeaking: false })
  let audio!: ReturnType<typeof useAudioQueue>
  function Harness() { audio = useAudioQueue(); return null }
  const root = createRoot(document.createElement('div'))
  try {
    await act(async () => root.render(<Harness />))
    await act(async () => useChatStore.getState().enqueueAudio({
      type: 'audio_chunk', duration_ms: 100, audio_base64: 'YQ==', emotion: 'neutral', text: 'Reply', visemes: [],
    }))
    expect(useChatStore.getState().isAISpeaking).toBe(true)
    await act(async () => { audio.stopPlayback(); useChatStore.getState().clearQueue() })
    await act(async () => resolveDecode({} as AudioBuffer))
    expect(createSource).not.toHaveBeenCalled()
    expect(useChatStore.getState().isAISpeaking).toBe(false)
  } finally {
    await act(async () => root.unmount())
  }
})
