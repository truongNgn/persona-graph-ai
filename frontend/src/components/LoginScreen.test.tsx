// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { LoginScreen } from './LoginScreen'

let root: Root
let container: HTMLDivElement

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  container = document.createElement('div')
  root = createRoot(container)
})
afterEach(async () => {
  await act(async () => root.unmount())
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('restores the quick login automatically when the backend becomes available', async () => {
  vi.stubGlobal('fetch', vi.fn()
    .mockRejectedValueOnce(new TypeError('Failed to fetch'))
    .mockResolvedValue({ ok: true, json: async () => ({ enabled: true }) }))
  await act(async () => root.render(<LoginScreen />))
  expect(container.textContent).toContain('Waiting for the local backend')
  expect(container.querySelector<HTMLButtonElement>('button')?.disabled).toBe(true)
  await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
  expect(container.textContent).toContain('Quick login as Dev Tester')
  expect(container.querySelector<HTMLButtonElement>('button')?.disabled).toBe(false)
})

it('hides quick login when the server explicitly disables it, including in a development build', async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ enabled: false }) })
  vi.stubGlobal('fetch', fetch)
  await act(async () => root.render(<LoginScreen />))
  expect(container.textContent).not.toContain('Quick login as Dev Tester')
  await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
  expect(fetch).toHaveBeenCalledTimes(1)
})
