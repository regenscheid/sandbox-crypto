// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';
import {
  KERNEL_DEADLINE_MS,
  KernelPoller,
  KernelReadError,
  kernelObservation,
  kernelPanelScope,
  onKernelLogout,
  type KernelObservation,
  type KernelPollState,
} from './kernels';
import KernelPanel from '../views/KernelPanel.svelte';

const scope = { projectId: 'p', sessionId: 's', environmentId: 'e' };
function observed(): KernelObservation {
  return {
    project_id: 'p',
    session_id: 's',
    environment_id: 'e',
    current_revision: 2,
    observed_at: '2026-10-12T22:00:00.123456+00:00',
    kernels: [
      {
        kernel_id: 'kernel-one',
        language: 'python',
        generation: 3,
        start_revision: 1,
        current_revision: 2,
        revision_status: 'predates',
      },
    ],
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function response(value: unknown = observed()): Response {
  return new Response(JSON.stringify(value));
}
const readers: KernelPoller[] = [];
function poll(read: (signal: AbortSignal) => Promise<KernelObservation>) {
  const states: KernelPollState[] = [];
  const reader = new KernelPoller(read, (state) => states.push(state));
  readers.push(reader);
  return { reader, states };
}
afterEach(() => {
  for (const reader of readers.splice(0)) reader.stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('exact passive observation validation', () => {
  it('retains exact safe pins and explicit unknowns; accepts a genuinely empty observation', () => {
    expect(kernelObservation(observed(), scope)).toEqual(observed());
    const unknown = observed();
    unknown.current_revision = null;
    unknown.kernels[0] = {
      ...unknown.kernels[0]!,
      start_revision: null,
      current_revision: null,
      revision_status: 'unknown',
    };
    expect(kernelObservation(unknown, scope).kernels[0]?.revision_status).toBe('unknown');
    expect(kernelObservation({ ...observed(), kernels: [] }, scope).kernels).toEqual([]);
  });
  it.each([
    [
      'project',
      (v: KernelObservation) => {
        v.project_id = 'q';
      },
    ],
    [
      'session',
      (v: KernelObservation) => {
        v.session_id = 'other';
      },
    ],
    [
      'environment',
      (v: KernelObservation) => {
        v.environment_id = 'other';
      },
    ],
    [
      'duplicate language',
      (v: KernelObservation) => {
        v.kernels.push({ ...v.kernels[0]!, kernel_id: 'second' });
      },
    ],
    [
      'duplicate ID',
      (v: KernelObservation) => {
        v.kernels.push({ ...v.kernels[0]!, language: 'sage' });
      },
    ],
    [
      'too many kernels',
      (v: KernelObservation) => {
        v.kernels.push(v.kernels[0]!, v.kernels[0]!);
      },
    ],
    [
      'ahead of current',
      (v: KernelObservation) => {
        v.kernels[0]!.start_revision = 3;
      },
    ],
    [
      'incoherent current',
      (v: KernelObservation) => {
        v.kernels[0]!.current_revision = 1;
      },
    ],
    [
      'false current',
      (v: KernelObservation) => {
        v.kernels[0]!.revision_status = 'current';
      },
    ],
    [
      'unsafe current',
      (v: KernelObservation) => {
        v.current_revision = 2 ** 53;
      },
    ],
    [
      'unsafe generation',
      (v: KernelObservation) => {
        v.kernels[0]!.generation = 2 ** 53;
      },
    ],
    [
      'fractional generation',
      (v: KernelObservation) => {
        v.kernels[0]!.generation = 1.5;
      },
    ],
    [
      'zero generation',
      (v: KernelObservation) => {
        v.kernels[0]!.generation = 0;
      },
    ],
    [
      'negative revision',
      (v: KernelObservation) => {
        v.current_revision = -1;
      },
    ],
    [
      'unsafe start',
      (v: KernelObservation) => {
        v.kernels[0]!.start_revision = 2 ** 53;
      },
    ],
    [
      'unscoped timestamp',
      (v: KernelObservation) => {
        v.observed_at = '2026-10-12T22:00:00';
      },
    ],
    [
      'invalid calendar',
      (v: KernelObservation) => {
        v.observed_at = '2026-02-31T22:00:00Z';
      },
    ],
  ])('refuses %s', (_name, change) => {
    const value = observed();
    change(value);
    expect(() => kernelObservation(value, scope)).toThrow(KernelReadError);
  });
  it('refuses unexpected fields, wrong scalar types and markup identifiers', () => {
    expect(() => kernelObservation({ ...observed(), unexpected: true }, scope)).toThrow();
    const value: unknown = { ...observed(), current_revision: '2' };
    expect(() => kernelObservation(value, scope)).toThrow();
    expect(() =>
      kernelObservation(
        { ...observed(), kernels: [{ ...observed().kernels[0], kernel_id: '<script>' }] },
        scope,
      ),
    ).toThrow();
  });
  it('mounts only the exact loaded open scope', () => {
    const detail = {
      project_id: 'p',
      session: { session_id: 's', environment_id: 'e', closed_at: null },
    };
    expect(kernelPanelScope('p', 's', detail)).toEqual(scope);
    expect(kernelPanelScope('p', 's', { ...detail, project_id: 'q' })).toBeNull();
    expect(
      kernelPanelScope('p', 's', {
        ...detail,
        session: { ...detail.session, session_id: 'other' },
      }),
    ).toBeNull();
    expect(
      kernelPanelScope('p', 's', {
        ...detail,
        session: { ...detail.session, closed_at: '2026-10-12T22:00:00Z' },
      }),
    ).toBeNull();
  });
});

describe('bounded narrow API', () => {
  it('uses only the same-origin passive route with no-store and redirects forbidden', async () => {
    const fetch = vi.fn().mockResolvedValue(response());
    vi.stubGlobal('fetch', fetch);
    expect(await api.kernels('p', 's', 'e', new AbortController().signal)).toEqual(observed());
    expect(fetch).toHaveBeenCalledOnce();
    expect(fetch.mock.calls[0]?.[0]).toBe('/api/v0/projects/p/sessions/s/kernels');
    expect(fetch.mock.calls[0]?.[1]).toMatchObject({
      redirect: 'error',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
  });
  it.each([401, 404, 422, 503])(
    'cancels arbitrary %i error bodies without parsing/retaining them',
    async (status) => {
      const cancel = vi.fn();
      const body = new ReadableStream<Uint8Array>({ cancel });
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { status })));
      await expect(api.kernels('p', 's', 'e', new AbortController().signal)).rejects.toMatchObject({
        status,
        message: 'Kernel observation unavailable',
      });
      expect(cancel).toHaveBeenCalledOnce();
    },
  );
  it('bounds streamed success bytes and number of chunks and cancels the reader', async () => {
    for (const chunks of [1, 257]) {
      const cancel = vi.fn();
      const body = new ReadableStream<Uint8Array>({
        pull(controller) {
          controller.enqueue(new Uint8Array(chunks === 1 ? 12 * 1024 + 1 : 0));
        },
        cancel,
      });
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)));
      await expect(api.kernels('p', 's', 'e', new AbortController().signal)).rejects.toBeInstanceOf(
        KernelReadError,
      );
      expect(cancel).toHaveBeenCalledOnce();
    }
  });
  it('bounds headers and body by the same deadline', async () => {
    vi.useFakeTimers();
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      cancel() {
        cancelled = true;
      },
    });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)));
    const request = api
      .kernels('p', 's', 'e', new AbortController().signal)
      .catch((error) => error);
    await vi.advanceTimersByTimeAsync(KERNEL_DEADLINE_MS);
    expect(await request).toBeInstanceOf(KernelReadError);
    expect(cancelled).toBe(true);
  });
  it('retains the one-request lease when an aborted header fetch ignores abort', async () => {
    vi.useFakeTimers();
    const pending = deferred<Response>();
    const fetch = vi.fn().mockReturnValue(pending.promise);
    vi.stubGlobal('fetch', fetch);
    const request = api
      .kernels('p', 's', 'e', new AbortController().signal)
      .catch((error) => error);
    await vi.advanceTimersByTimeAsync(KERNEL_DEADLINE_MS);
    await expect(api.kernels('p', 's', 'e', new AbortController().signal)).rejects.toBeInstanceOf(
      KernelReadError,
    );
    expect(fetch).toHaveBeenCalledOnce();
    pending.resolve(response());
    expect(await request).toBeInstanceOf(KernelReadError);
    fetch.mockResolvedValue(response());
    expect(await api.kernels('p', 's', 'e', new AbortController().signal)).toEqual(observed());
  });
  it('refuses malformed, redirected, invalid UTF-8 and foreign-scope responses', async () => {
    for (const value of [
      new Response('not-json'),
      new Response(new Uint8Array([255])),
      response({ ...observed(), session_id: 'foreign' }),
    ]) {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(value));
      await expect(api.kernels('p', 's', 'e', new AbortController().signal)).rejects.toBeInstanceOf(
        KernelReadError,
      );
    }
    const redirected = response();
    Object.defineProperty(redirected, 'redirected', { value: true });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(redirected));
    await expect(api.kernels('p', 's', 'e', new AbortController().signal)).rejects.toBeInstanceOf(
      KernelReadError,
    );
  });
});

describe('visible-session request ownership', () => {
  it('starts only when visible and waits three seconds after completion, with no overlap', async () => {
    vi.useFakeTimers();
    const pending = deferred<KernelObservation>();
    const read = vi.fn().mockReturnValue(pending.promise);
    const { reader, states } = poll(read);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(read).not.toHaveBeenCalled();
    reader.setVisible(true);
    await vi.advanceTimersByTimeAsync(1000);
    expect(read).toHaveBeenCalledOnce();
    pending.resolve(observed());
    await vi.advanceTimersByTimeAsync(0);
    expect(states.at(-1)?.phase).toBe('observed');
    await vi.advanceTimersByTimeAsync(2999);
    expect(read).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    expect(read).toHaveBeenCalledTimes(2);
  });
  it('invalidates hide/unmount results even when abort is ignored; visible return waits for ownership', async () => {
    vi.useFakeTimers();
    const first = deferred<KernelObservation>();
    const read = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(observed());
    const { reader, states } = poll(read);
    reader.setVisible(true);
    reader.setVisible(false);
    expect(read.mock.calls[0]?.[0].aborted).toBe(true);
    reader.setVisible(true);
    expect(read).toHaveBeenCalledOnce();
    first.resolve({ ...observed(), current_revision: null });
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(2);
    expect(
      states.filter((s) => s.phase === 'observed').map((s) => s.data?.current_revision),
    ).toEqual([2]);
    reader.stop();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledTimes(2);
  });
  it('marks ignored-abort timeout unavailable while retaining the actual request and rejecting late data', async () => {
    vi.useFakeTimers();
    const pending = deferred<KernelObservation>();
    const read = vi.fn().mockReturnValue(pending.promise);
    const { reader, states } = poll(read);
    reader.setVisible(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledOnce();
    expect(states.at(-1)?.phase).toBe('unavailable');
    pending.resolve(observed());
    await vi.advanceTimersByTimeAsync(0);
    expect(states.some((s) => s.phase === 'observed')).toBe(false);
  });
  it('uses bounded backoff and resets cadence after recovery without treating failure as empty', async () => {
    vi.useFakeTimers();
    const times: number[] = [];
    const read = vi.fn(async (): Promise<KernelObservation> => {
      times.push(Date.now());
      throw new KernelReadError(503);
    });
    const { reader, states } = poll(read);
    reader.setVisible(true);
    await vi.advanceTimersByTimeAsync(102_000);
    expect(times.slice(1).map((at, index) => at - times[index]!)).toEqual([
      6000, 12000, 24000, 30000, 30000,
    ]);
    expect(states.at(-1)).toEqual({ phase: 'unavailable', data: null });
    read.mockImplementation(async () => {
      times.push(Date.now());
      return observed();
    });
    await vi.advanceTimersByTimeAsync(30_000);
    expect(states.at(-1)?.phase).toBe('observed');
    const count = read.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3000);
    expect(read).toHaveBeenCalledTimes(count + 1);
  });
  it.each([401, 404])('stops on %i and clears old data', async (status) => {
    vi.useFakeTimers();
    const read = vi
      .fn()
      .mockResolvedValueOnce(observed())
      .mockRejectedValue(new KernelReadError(status));
    const { reader, states } = poll(read);
    reader.setVisible(true);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(read).toHaveBeenCalledTimes(2);
    expect(states.at(-1)).toEqual({ phase: status === 401 ? 'signed_out' : 'missing', data: null });
  });
  it('invalidates at logout initiation even if logout fails and an observation completes later', async () => {
    vi.useFakeTimers();
    const pending = deferred<KernelObservation>(),
      loggingOut = deferred<Response>();
    const { reader, states } = poll(() => pending.promise);
    const off = onKernelLogout(() => reader.logout());
    reader.setVisible(true);
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(loggingOut.promise));
    const logout = api.logout().catch((error) => error);
    expect(states.at(-1)).toEqual({ phase: 'signed_out', data: null });
    const lateRead = vi.fn().mockResolvedValue(observed());
    const late = poll(lateRead),
      offLate = onKernelLogout(() => late.reader.logout());
    late.reader.setVisible(true);
    expect(lateRead).not.toHaveBeenCalled();
    expect(late.states.at(-1)?.phase).toBe('signed_out');
    await expect(api.kernels('p', 's', 'e', new AbortController().signal)).rejects.toMatchObject({
      status: 401,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    pending.resolve(observed());
    loggingOut.resolve(new Response('', { status: 503 }));
    await logout;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(states.at(-1)?.phase).toBe('signed_out');
    off();
    offLate();
  });
});

describe('kernel panel server rendering', () => {
  function html(state: KernelPollState): string {
    return render(KernelPanel, { props: { ...scope, initialState: state } }).body;
  }
  it('shows exact revision/generation, predates and receipt-time limits without a restart action', () => {
    const text = html({ phase: 'observed', data: observed() });
    expect(text).toContain('python: predates');
    expect(text).toContain('Generation');
    expect(text).toContain('>3<');
    expect(text).toContain('start revision');
    expect(text).toContain('>1<');
    expect(text).toContain('current revision');
    expect(text).toContain('2026-10-12T22:00:00.123456+00:00');
    expect(text).toContain('not an atomic state or freshness guarantee');
    expect(text).not.toContain('<button');
    expect(text).not.toContain('Restart');
  });
  it('distinguishes unavailable/unknown from observed empty and labels previous data', () => {
    expect(html({ phase: 'unavailable', data: null })).toContain('current kernel state is unknown');
    expect(html({ phase: 'unconfigured', data: null })).toContain('unconfigured');
    expect(html({ phase: 'observed', data: { ...observed(), kernels: [] } })).toContain(
      'No kernels were observed',
    );
    const previous = html({ phase: 'unavailable', data: { ...observed(), kernels: [] } });
    expect(previous).toContain('Previous observation; current state is unknown');
    expect(previous).not.toContain('No kernels were observed');
  });
  it('escapes text, renders current/unknown honestly and performs no SSR request', () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    const data = observed();
    data.kernels[0] = {
      ...data.kernels[0]!,
      kernel_id: '<img src=x>',
      revision_status: 'unknown',
      start_revision: null,
    };
    const text = html({ phase: 'observed', data });
    expect(text).not.toContain('<img');
    expect(text).toContain('&lt;img');
    expect(text).toContain('python: unknown');
    expect(text).toContain('relationship is unknown');
    expect(fetch).not.toHaveBeenCalled();
  });
});
