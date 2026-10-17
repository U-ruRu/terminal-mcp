import { afterEach,expect,test,vi } from 'vitest'
import { ConsoleClient,ConsoleHttpError,type FetchLike } from './client'

const pending = <T,>(): Promise<T> => new Promise(() => undefined)
afterEach(() => { vi.useRealTimers() })

test('a fetch ignoring cancellation is bounded and diagnostics do not contain tokens or query strings', async () => {
  vi.useFakeTimers()
  const fetcher = vi.fn<FetchLike>(() => pending<Response>())
  const client = new ConsoleClient('https://terminal.example', () => 'synthetic-token', fetcher, 200)
  const result = client.accessMeshSlot('synthetic-slot').catch((error: unknown) => error)
  await vi.advanceTimersByTimeAsync(200)
  const error = await result
  expect(error).toBeInstanceOf(ConsoleHttpError)
  expect(error).toMatchObject({ code: 'request_timeout', status: 0, endpoint: '/actions/access/slots/:slot_id', elapsedMs: 200 })
  expect(JSON.stringify(error)).not.toContain('synthetic-token')
  expect(JSON.stringify(error)).not.toContain('synthetic-slot')
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true)
})

test('successful headers do not stop the deadline while JSON body is hanging', async () => {
  vi.useFakeTimers()
  const fetcher: FetchLike = async () => ({ ok: true, status: 200, json: () => pending<unknown>() }) as Response
  const client = new ConsoleClient('https://terminal.example', () => '', fetcher, 300)
  const result = client.accessMeshDefaults().catch((error: unknown) => error)
  await vi.advanceTimersByTimeAsync(300)
  expect(await result).toMatchObject({ code: 'request_timeout', endpoint: '/actions/access/defaults' })
})
