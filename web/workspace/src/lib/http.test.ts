// SPDX-License-Identifier: MIT
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, get, inScope, post } from './http';

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status });
}
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('one request function', () => {
  it('asks this origin under /api/v0 without the cache or redirects', async () => {
    const fetch = vi.fn().mockResolvedValue(json({ ok: true }));
    vi.stubGlobal('fetch', fetch);
    expect(await get('/projects')).toEqual({ ok: true });
    expect(fetch.mock.calls[0]?.[0]).toBe('/api/v0/projects');
    expect(fetch.mock.calls[0]?.[1]).toMatchObject({
      method: 'GET',
      redirect: 'error',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
  });
  it('sends a POST body as JSON', async () => {
    const fetch = vi.fn().mockResolvedValue(json({ done: 1 }));
    vi.stubGlobal('fetch', fetch);
    expect(await post('/x', { a: 1 })).toEqual({ done: 1 });
    expect(fetch.mock.calls[0]?.[1]).toMatchObject({
      method: 'POST',
      body: '{"a":1}',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    });
  });
  it('reads the error envelope, its details and its other fields', async () => {
    const error = {
      kind: 'conflict',
      message: 'stale base',
      details: { revision: 3 },
      current: { plan_id: 'p', version: 2 },
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ error }, 409)));
    const found = (await get('/x').catch((e: unknown) => e)) as ApiError;
    expect(found).toBeInstanceOf(ApiError);
    expect(found).toMatchObject({ status: 409, kind: 'conflict', message: 'stale base' });
    expect(found.details).toEqual({ revision: 3 });
    expect(found.error.current).toEqual({ plan_id: 'p', version: 2 });
  });
  it('keeps the status of an error that is not JSON', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>', { status: 502 })));
    await expect(get('/x')).rejects.toMatchObject({ status: 502, kind: 'unavailable' });
  });
  it('fails with status 0 for no answer, malformed JSON or invalid UTF-8', async () => {
    for (const answer of [
      Promise.reject(new TypeError('offline')),
      Promise.resolve(new Response('not-json')),
      Promise.resolve(new Response(new Uint8Array([255]))),
    ]) {
      answer.catch(() => undefined);
      vi.stubGlobal('fetch', vi.fn().mockReturnValue(answer));
      await expect(get('/x')).rejects.toMatchObject({ status: 0, kind: 'unavailable' });
    }
  });
  it('refuses an answer over its byte limit and cancels the body', async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        controller.enqueue(new Uint8Array(1024));
      },
      cancel,
    });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)));
    await expect(get('/x', { maxBytes: 4096 })).rejects.toMatchObject({ status: 0 });
    expect(cancel).toHaveBeenCalledOnce();
  });
  it('stops at its deadline and when its signal aborts', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn(
      (_path: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) =>
          init.signal?.addEventListener('abort', () => reject(new DOMException('aborted'))),
        ),
    );
    vi.stubGlobal('fetch', fetch);
    const late = get('/x', { deadlineMs: 1000 }).catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(1000);
    expect(await late).toMatchObject({ status: 0, message: 'the request was stopped' });
    const controller = new AbortController();
    const stopped = get('/x', { signal: controller.signal }).catch((e: unknown) => e);
    controller.abort();
    expect(await stopped).toMatchObject({ status: 0 });
  });
  it('refuses an answer for another scope', () => {
    expect(inScope({ project_id: 'p', x: 1 }, { project_id: 'p' })).toEqual({
      project_id: 'p',
      x: 1,
    });
    expect(() => inScope({ project_id: 'q' }, { project_id: 'p' })).toThrow(ApiError);
    expect(() => inScope(null, { project_id: 'p' })).toThrow(ApiError);
  });
  // Last: a 401 ends the page's session for the rest of this file.
  it('a 401 signs the page out, and no later request starts', async () => {
    const fetch = vi.fn().mockResolvedValue(json({ error: { kind: 'policy_denied' } }, 401));
    vi.stubGlobal('fetch', fetch);
    await expect(get('/x')).rejects.toMatchObject({ status: 401, loggedOut: true });
    await expect(get('/y')).rejects.toMatchObject({ status: 401 });
    expect(fetch).toHaveBeenCalledOnce();
  });
});
