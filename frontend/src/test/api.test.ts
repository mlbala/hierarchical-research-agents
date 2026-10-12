import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from '@/lib/api'

function mockFetch(response: Response | Error) {
  const fn = vi.fn(async () => {
    if (response instanceof Error) throw response
    return response
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

afterEach(() => vi.unstubAllGlobals())

describe('api client', () => {
  it('sends JSON plus the CSRF guard header on mutations', async () => {
    const fetchMock = mockFetch(new Response(JSON.stringify({ id: 'p1' }), { status: 201 }))
    await api.createProject({ name: 'X', description: '' })
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/v1/projects')
    const headers = init.headers as Record<string, string>
    expect(init.method).toBe('POST')
    expect(headers['X-Research-Hive']).toBe('1')
    expect(headers['Content-Type']).toBe('application/json')
  })

  it('passes the idempotency key when creating runs', async () => {
    const fetchMock = mockFetch(new Response(JSON.stringify({ id: 'r1' }), { status: 201 }))
    await api.createRun('p1', { question: 'q'.repeat(12), input_urls: [], depth: 'quick' }, 'key-123456')
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe('key-123456')
  })

  it('turns the server error envelope into an ApiError', async () => {
    mockFetch(
      new Response(
        JSON.stringify({
          error: { code: 'QUEUE_FULL', message: 'The research queue is full.', request_id: 'abc', retryable: true },
        }),
        { status: 429 },
      ),
    )
    const error = await api.getRun('r1').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 429, code: 'QUEUE_FULL', requestId: 'abc', retryable: true })
  })

  it('reports network failures without leaking internals', async () => {
    mockFetch(new TypeError('Failed to fetch'))
    const error = await api.config().catch((e: unknown) => e)
    expect(error).toMatchObject({ code: 'NETWORK_ERROR', status: 0 })
  })
})
