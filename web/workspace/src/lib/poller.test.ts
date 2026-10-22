// SPDX-License-Identifier: MIT
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './http';
import { Poller, POLL_DEADLINE_MS, type PollOptions, type PollState } from './poller';
import { signOut } from './session';

interface Data {
  n: number;
  closed?: boolean;
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
}
const pollers: Poller<Data>[] = [];
function poll(read: (signal: AbortSignal) => Promise<Data>, options: PollOptions<Data> = {}) {
  const states: PollState<Data>[] = [];
  const poller = new Poller(read, (state) => states.push(state), options);
  pollers.push(poller);
  return { poller, states };
}
const settled = (states: PollState<Data>[]) => states.filter((s) => !s.busy);
afterEach(() => {
  for (const poller of pollers.splice(0)) poller.stop();
  vi.useRealTimers();
});

describe('visible polling', () => {
  it('reads only while visible, three seconds after each read, never overlapping', async () => {
    vi.useFakeTimers();
    const pending = deferred<Data>();
    const read = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue({ n: 2 });
    const { poller, states } = poll(read);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(read).not.toHaveBeenCalled();
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(1000);
    expect(read).toHaveBeenCalledOnce();
    expect(states.at(-1)).toMatchObject({ phase: 'loading', busy: true });
    pending.resolve({ n: 1 });
    await vi.advanceTimersByTimeAsync(0);
    expect(states.at(-1)).toEqual({ phase: 'observed', data: { n: 1 }, busy: false });
    await vi.advanceTimersByTimeAsync(2999);
    expect(read).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    expect(read).toHaveBeenCalledTimes(2);
  });
  it('drops a reply that arrives after the page was hidden, even if abort is ignored', async () => {
    vi.useFakeTimers();
    const first = deferred<Data>();
    const read = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue({ n: 2 });
    const { poller, states } = poll(read);
    poller.setVisible(true);
    poller.setVisible(false);
    expect((read.mock.calls[0]?.[0] as AbortSignal).aborted).toBe(true);
    poller.setVisible(true);
    expect(read).toHaveBeenCalledOnce();
    first.resolve({ n: 1 });
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(2);
    expect(
      settled(states)
        .filter((s) => s.phase === 'observed')
        .map((s) => s.data?.n),
    ).toEqual([2]);
  });
  it('is unavailable at its deadline and drops the late reply', async () => {
    vi.useFakeTimers();
    const pending = deferred<Data>();
    const { poller, states } = poll(() => pending.promise, { deadlineMs: 5000 });
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(5000);
    expect(states.at(-1)?.phase).toBe('unavailable');
    pending.resolve({ n: 1 });
    await vi.advanceTimersByTimeAsync(0);
    expect(states.some((s) => s.phase === 'observed')).toBe(false);
  });
  it('backs off to thirty seconds, keeps earlier data, and recovers its cadence', async () => {
    vi.useFakeTimers();
    const times: number[] = [];
    const read = vi.fn(async (): Promise<Data> => {
      times.push(Date.now());
      if (times.length === 1) return { n: 1 };
      throw new ApiError(503, 'unavailable', 'down');
    });
    const { poller, states } = poll(read);
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(100_000);
    expect(times.slice(1).map((at, i) => at - times[i]!)).toEqual([
      3000, 6000, 12000, 24000, 30000,
    ]);
    expect(settled(states).at(-1)).toEqual({ phase: 'unavailable', data: { n: 1 }, busy: false });
    read.mockImplementation(async () => {
      times.push(Date.now());
      return { n: 2 };
    });
    await vi.advanceTimersByTimeAsync(30_000);
    expect(states.at(-1)?.phase).toBe('observed');
    const count = read.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3000);
    expect(read).toHaveBeenCalledTimes(count + 1);
  });
  it.each([
    [401, 'signed_out', null],
    [404, 'missing', null],
    [409, 'changed', { n: 1 }],
  ] as const)('stops for good on %i', async (status, phase, data) => {
    vi.useFakeTimers();
    const read = vi
      .fn()
      .mockResolvedValueOnce({ n: 1 })
      .mockRejectedValue(new ApiError(status, 'x', 'y'));
    const { poller, states } = poll(read);
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledTimes(2);
    expect(states.at(-1)).toEqual({ phase, data, busy: false });
  });
  it('reports 422 as unsupported and keeps trying', async () => {
    vi.useFakeTimers();
    const read = vi.fn().mockRejectedValue(new ApiError(422, 'unsupported', 'no worker'));
    const { poller, states } = poll(read);
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(read).toHaveBeenCalledTimes(2);
    expect(states.at(-1)?.phase).toBe('unsupported');
  });
});

describe('options', () => {
  it('endOn: retries statuses the poller does not end on, but a 401 always ends it', async () => {
    vi.useFakeTimers();
    const read = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(404, 'not_found', 'no environment yet'))
      .mockRejectedValueOnce(new ApiError(409, 'conflict', 'another project'))
      .mockResolvedValueOnce({ n: 1 })
      .mockRejectedValue(new ApiError(401, 'policy_denied', 'signed out'));
    const { poller, states } = poll(read, { endOn: {} });
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(6000);
    expect(read).toHaveBeenCalledTimes(2);
    expect(settled(states).at(-1)?.phase).toBe('unavailable');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledTimes(4);
    expect(states.at(-1)).toEqual({ phase: 'signed_out', data: null, busy: false });
  });
  it('waitOn null: a status that means "not yet" leaves the view as it was, with no backoff', async () => {
    vi.useFakeTimers();
    const times: number[] = [];
    const read = vi.fn(async (): Promise<Data> => {
      times.push(Date.now());
      if (times.length === 1 || times.length === 4) return { n: times.length };
      throw new ApiError(429, 'unavailable', 'busy');
    });
    const { poller, states } = poll(read, { waitOn: { 429: null } });
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(12_000);
    // Refusals never slow the next read: it stays three seconds away.
    expect(times.slice(1).map((at, i) => at - times[i]!)).toEqual([3000, 3000, 3000, 3000]);
    expect(settled(states).map((s) => s.phase)).not.toContain('unavailable');
    // The last read, a refusal after data, kept that data and its phase.
    expect(settled(states).at(-1)).toEqual({ phase: 'observed', data: { n: 4 }, busy: false });
  });
  it('waitOn null: before the first answer the view is still loading, not unavailable', async () => {
    vi.useFakeTimers();
    const read = vi.fn().mockRejectedValue(new ApiError(429, 'unavailable', 'busy'));
    const { poller, states } = poll(read, { waitOn: { 429: null } });
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(read.mock.calls.length).toBeGreaterThan(2);
    expect(settled(states).at(-1)).toEqual({ phase: 'loading', data: null, busy: false });
  });
  it('waitOn phase: shows that phase and keeps asking, past an endOn for the status', async () => {
    vi.useFakeTimers();
    const read = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(404, 'not_found', 'not yet'))
      .mockRejectedValueOnce(new ApiError(404, 'not_found', 'not yet'))
      .mockResolvedValue({ n: 1 });
    const { poller, states } = poll(read, { waitOn: { 404: 'missing' } });
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(3100);
    expect(read).toHaveBeenCalledTimes(2);
    expect(settled(states).at(-1)).toEqual({ phase: 'missing', data: null, busy: false });
    await vi.advanceTimersByTimeAsync(3000);
    expect(settled(states).at(-1)).toEqual({ phase: 'observed', data: { n: 1 }, busy: false });
  });
  it('waitOn: does not mask a real failure, and a 401 still ends the poller', async () => {
    vi.useFakeTimers();
    const read = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(503, 'unavailable', 'down'))
      .mockRejectedValueOnce(new ApiError(429, 'unavailable', 'busy'))
      .mockRejectedValue(new ApiError(401, 'policy_denied', 'signed out'));
    const { poller, states } = poll(read, { waitOn: { 429: null, 401: 'loading' } });
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(100);
    expect(settled(states).at(-1)?.phase).toBe('unavailable');
    await vi.advanceTimersByTimeAsync(6000); // the 503 backed off; the refusal changes nothing
    expect(read).toHaveBeenCalledTimes(2);
    expect(settled(states).at(-1)?.phase).toBe('unavailable');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledTimes(3);
    expect(states.at(-1)).toEqual({ phase: 'signed_out', data: null, busy: false });
  });
  it('done: the final state is not busy', async () => {
    vi.useFakeTimers();
    const { poller, states } = poll(vi.fn().mockResolvedValue({ n: 1, closed: true }), {
      done: (data) => data.closed === true,
    });
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(states.at(-1)).toEqual({ phase: 'observed', data: { n: 1, closed: true }, busy: false });
  });
  it('once: a read that hiding the page interrupted is read again when it is shown', async () => {
    vi.useFakeTimers();
    const first = deferred<Data>();
    const read = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue({ n: 2 });
    const { poller, states } = poll(read, { once: true });
    poller.setVisible(true);
    poller.setVisible(false);
    first.resolve({ n: 1 });
    await vi.advanceTimersByTimeAsync(0);
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(read).toHaveBeenCalledTimes(2);
    expect(settled(states).at(-1)).toEqual({ phase: 'observed', data: { n: 2 }, busy: false });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledTimes(2);
  });
  it('once: reads a single time, and failure leaves it unavailable', async () => {
    vi.useFakeTimers();
    const ok = poll(vi.fn().mockResolvedValue({ n: 1 }), { once: true });
    ok.poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(settled(ok.states).at(-1)).toMatchObject({ phase: 'observed' });
    const failing = vi.fn().mockRejectedValue(new ApiError(503, 'x', 'y'));
    const bad = poll(failing, { once: true });
    bad.poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(failing).toHaveBeenCalledOnce();
    expect(bad.states.at(-1)?.phase).toBe('unavailable');
  });
  it('done: stops after observing the final state', async () => {
    vi.useFakeTimers();
    const read = vi
      .fn()
      .mockResolvedValueOnce({ n: 1 })
      .mockResolvedValueOnce({ n: 2, closed: true })
      .mockResolvedValue({ n: 3 });
    const { poller, states } = poll(read, { done: (data) => data.closed === true });
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledTimes(2);
    expect(states.at(-1)?.data).toEqual({ n: 2, closed: true });
  });
  it('repeat false: reads again only after a visibility change', async () => {
    vi.useFakeTimers();
    const read = vi.fn().mockResolvedValue({ n: 1 });
    const { poller } = poll(read, { repeat: false });
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledOnce();
    poller.setVisible(false);
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(2);
  });
});

describe('sign-out', () => {
  // Last: signing out ends the page's session for the rest of this file.
  it('stops every watching poller, drops late replies and starts no new read', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('document', {
      visibilityState: 'visible',
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    });
    const pending = deferred<Data>();
    const { poller, states } = poll(() => pending.promise);
    const stop = poller.watch();
    signOut();
    expect(states.at(-1)).toEqual({ phase: 'signed_out', data: null, busy: false });
    pending.resolve({ n: 1 });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(states.at(-1)?.phase).toBe('signed_out');
    const lateRead = vi.fn().mockResolvedValue({ n: 2 });
    const late = poll(lateRead);
    const stopLate = late.poller.watch();
    expect(lateRead).not.toHaveBeenCalled();
    expect(late.states.at(-1)?.phase).toBe('signed_out');
    stop();
    stopLate();
    vi.unstubAllGlobals();
  });
});

it('waits long enough for a workspace a busy host slows down', () => {
  // An investigation's computations often keep the host's CPUs busy; at 8 s, a slow read
  // showed as "unavailable" (Mac, 2026-10-21).
  expect(POLL_DEADLINE_MS).toBeGreaterThanOrEqual(30_000);
});
