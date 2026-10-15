// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  KERNEL_OBSERVATION_MAX_BYTES,
  readKernels,
  kernelPanelScope,
  type KernelObservation,
} from './kernels';
import { Poller, type PollState } from './poller';
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
        run: null,
        start_revision: 1,
        current_revision: 2,
        revision_status: 'predates',
      },
    ],
  };
}
function response(value: unknown = observed()): Response {
  return new Response(JSON.stringify(value));
}
/** The largest observation the contract allows: 64 runs' kernels, every ID at its longest. */
function largest(): KernelObservation {
  const id = (n: number) => String(n).padStart(128, '0');
  const kernels = Array.from({ length: 128 }, (_, n) => ({
    ...observed().kernels[0]!,
    kernel_id: id(n),
    language: n % 2 ? ('sage' as const) : ('python' as const),
    run: id(n >> 1),
    generation: 2 ** 31 - 1,
  }));
  return { ...observed(), project_id: id(0), session_id: id(1), environment_id: id(2), kernels };
}
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('kernel scope', () => {
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

describe('kernel reads', () => {
  it('reads its own route and refuses an observation of another scope', async () => {
    const fetch = vi.fn().mockResolvedValue(response());
    vi.stubGlobal('fetch', fetch);
    expect(await readKernels(scope, new AbortController().signal)).toEqual(observed());
    expect(fetch.mock.calls[0]?.[0]).toBe('/api/v0/projects/p/sessions/s/kernels');
    fetch.mockResolvedValue(response({ ...observed(), environment_id: 'other' }));
    await expect(readKernels(scope, new AbortController().signal)).rejects.toMatchObject({
      status: 0,
    });
  });
  it('reads an observation of many runs, past the old 12 KiB limit, up to the server bound', async () => {
    const big = largest();
    const text = JSON.stringify(big);
    expect(text.length).toBeGreaterThan(12 * 1024);
    expect(text.length).toBeLessThan(KERNEL_OBSERVATION_MAX_BYTES);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(big)));
    const found = await readKernels(
      { projectId: big.project_id, sessionId: big.session_id, environmentId: big.environment_id },
      new AbortController().signal,
    );
    expect(found.kernels).toHaveLength(128);
    expect(found).toEqual(big);
  });
  it('refuses an observation over the limit with the too-large error', async () => {
    const padded = JSON.stringify(observed()) + ' '.repeat(KERNEL_OBSERVATION_MAX_BYTES);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() => Promise.resolve(new Response(padded))),
    );
    await expect(readKernels(scope, new AbortController().signal)).rejects.toMatchObject({
      status: 0,
      kind: 'unavailable',
      message: 'the answer is too large',
    });
  });
});

describe('kernel panel reading an answer that is too large', () => {
  it('ends unavailable with the state unknown, not blank', async () => {
    const padded = JSON.stringify(observed()) + ' '.repeat(KERNEL_OBSERVATION_MAX_BYTES);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() => Promise.resolve(new Response(padded))),
    );
    const states: PollState<KernelObservation>[] = [];
    const poller = new Poller(
      (signal) => readKernels(scope, signal),
      (state) => states.push(state),
    );
    poller.setVisible(true);
    await vi.waitFor(() =>
      expect(states.at(-1)).toMatchObject({ phase: 'unavailable', busy: false }),
    );
    poller.stop();
    const text = render(KernelPanel, { props: { ...scope, initialState: states.at(-1)! } }).body;
    expect(text).toContain('Kernel observations unavailable');
    expect(text).toContain('current kernel state is unknown');
    expect(text).not.toContain('No kernels were observed');
  });
});

describe('kernel panel server rendering', () => {
  function html(state: PollState<KernelObservation>): string {
    return render(KernelPanel, { props: { ...scope, initialState: state } }).body;
  }
  it('shows exact revision/generation, predates and receipt-time limits without a restart action', () => {
    const text = html({ phase: 'observed', data: observed(), busy: false });
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
    expect(html({ phase: 'unavailable', data: null, busy: false })).toContain(
      'current kernel state is unknown',
    );
    expect(html({ phase: 'unsupported', data: null, busy: false })).toContain('unconfigured');
    expect(
      html({ phase: 'observed', data: { ...observed(), kernels: [] }, busy: false }),
    ).toContain('No kernels were observed');
    const previous = html({
      phase: 'unavailable',
      data: { ...observed(), kernels: [] },
      busy: false,
    });
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
    const text = html({ phase: 'observed', data, busy: false });
    expect(text).not.toContain('<img');
    expect(text).toContain('&lt;img');
    expect(text).toContain('python: unknown');
    expect(text).toContain('relationship is unknown');
    expect(fetch).not.toHaveBeenCalled();
  });
});
