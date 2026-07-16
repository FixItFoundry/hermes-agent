import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { chatMessageText } from '@/lib/chat-messages'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'session-1'

let handleEvent: ((event: RpcEvent) => void) | null = null
let sessionStates: Map<string, ClientSessionState>

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const current = sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState()
      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)
      // Mirror into the test-accessible map
      sessionStates.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

async function mountStream() {
  sessionStates = new Map()
  render(<Harness />)
  await waitFor(() => expect(handleEvent).not.toBeNull())
}

const start = () => act(() => handleEvent!({ payload: {}, session_id: SID, type: 'message.start' }))
const delta = (text: string) => act(() => handleEvent!({ payload: { text }, session_id: SID, type: 'message.delta' }))
const interim = (text: string) =>
  act(() => handleEvent!({ payload: { text, already_streamed: true }, session_id: SID, type: 'message.interim' }))
const complete = (text: string) =>
  act(() => handleEvent!({ payload: { text }, session_id: SID, type: 'message.complete' }))

function getState(): ClientSessionState {
  // The Harness stores state in sessionStateByRuntimeIdRef; we can't access it
  // directly, so we capture it from the updateSessionState callback.
  return sessionStates.get(SID) ?? createClientSessionState()
}

function assistantText(): string {
  const state = getState()
  const last = [...state.messages].reverse().find(m => m.role === 'assistant' && !m.hidden)
  return last ? chatMessageText(last) : ''
}

describe('useMessageStream interim text sealing', () => {
  beforeEach(() => {
    handleEvent = null
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('preserves interim text that the final response does not include', async () => {
    await mountStream()
    await start()

    // Model streams its first (interim) answer
    await delta('awaaaaa clean!! tsc zero errors')
    // Interim callback seals it so it survives completion
    await interim('awaaaaa clean!! tsc zero errors')

    // Turn completes with a different final response (e.g. after verify-on-stop)
    await complete('All checks passed.')

    const text = assistantText()
    // Both the interim text AND the final text should be present
    expect(text).toContain('awaaaaa clean!! tsc zero errors')
    expect(text).toContain('All checks passed.')
  })

  it('dedupes interim text when the final response includes it', async () => {
    await mountStream()
    await start()

    await delta('Let me check the files.')
    await interim('Let me check the files.')

    // Final response repeats the interim text + adds more
    await complete('Let me check the files. Everything looks good.')

    const text = assistantText()
    // The interim text should NOT appear separately — it was folded into the final
    expect(text).not.toContain('Let me check the files.Let me check the files.')
    expect(text).toContain('Let me check the files. Everything looks good.')
  })

  it('clears sealed text at turn end so the next turn starts clean', async () => {
    await mountStream()
    await start()

    await delta('interim text')
    await interim('interim text')
    await complete('final text')

    // Second turn
    await start()
    await delta('new turn text')
    await complete('new turn final')

    const text = assistantText()
    expect(text).toBe('new turn final')
  })
})
