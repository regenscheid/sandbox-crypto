// SPDX-License-Identifier: MIT
// The Compute tab's kernel list with each kernel's current cell (B10, item 5), mounted: the
// session's kernel shows the main run's python call, with its purpose as the agent's words
// and a hidden character as its code point; a kernel with no call waiting is idle; a call
// whose kernel the observation lacks stays under "Running now" with a note.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import SessionCompute from '../views/SessionCompute.svelte';
import type { KernelObservation } from './kernels';
import type { RunEvent, RunTree, TreeRun } from './api-types';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle(): Promise<void> {
  for (let i = 0; i < 12; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

const run = (name: string, fields: Partial<TreeRun> = {}): TreeRun => ({
  name,
  run_id: `id-${name}`,
  role: 'delegated',
  parent: 'main',
  delegation: null,
  started_at: '2026-10-25T10:00:00Z',
  state: 'running',
  stopped: null,
  kept: true,
  alias: null,
  limits: null,
  tool_mix: null,
  ...fields,
});
const tree: RunTree = {
  runs: [run('main', { role: 'main', parent: null }), run('child')],
  delegations: [],
  delegations_total: 0,
};

const assistant = (calls: object[]): RunEvent => ({
  seq: 1,
  at: '2026-10-25T10:01:00Z',
  kind: 'assistant',
  data: { text: '', tool_calls: calls },
  pins: [],
});
const call = (id: string, name: string, args: object, purpose?: string) => ({
  id,
  name,
  arguments: JSON.stringify(args),
  ...(purpose ? { purpose } : {}),
});

const observation = (kernels: KernelObservation['kernels']): KernelObservation => ({
  project_id: 'p',
  session_id: 's',
  environment_id: 'e',
  current_revision: 1,
  observed_at: '2026-10-25T10:02:00Z',
  kernels,
});
const kernel = (id: string, language: 'python' | 'sage', owner: string | null) => ({
  kernel_id: id,
  language,
  generation: 1,
  run: owner,
  start_revision: 1,
  current_revision: 1,
  revision_status: 'current' as const,
});

function serve(events: Record<string, RunEvent[]>, kernels: KernelObservation) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const found = /\/runs\/([^/]+)\/events/.exec(url);
      if (found)
        return new Response(JSON.stringify({ state: 'active', events: events[found[1]!] ?? [] }));
      if (url.includes('/kernels')) return new Response(JSON.stringify(kernels));
      return new Response('{}', { status: 500 });
    }),
  );
}

function show(): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(SessionCompute, {
      target,
      props: {
        projectId: 'p',
        sessionId: 's',
        tree,
        kernelScope: { projectId: 'p', sessionId: 's', environmentId: 'e' },
      },
    }),
  );
  return target;
}

it("shows a kernel's purpose as the agent's words, with a hidden character as its code point", async () => {
  serve(
    {
      main: [
        assistant([
          call('c1', 'python', { code: 'import time\ntime.sleep(60)' }, 'Check q = 2^8‮ now'),
        ]),
      ],
    },
    observation([kernel('k-session', 'python', null), kernel('k-child', 'python', 'id-child')]),
  );
  const target = show();
  await settle();
  const running = target.querySelector('[data-kernel="k-session"]')!;
  expect(running.textContent).toContain("the agent's words:");
  expect(running.textContent).toContain('Check q = 2^8⟦U+202E⟧ now');
  expect(running.textContent).not.toContain('‮');
  expect(running.textContent).toContain('Research agent');
  // The delegated run’s own kernel has no call waiting.
  const idle = target.querySelector('[data-kernel="k-child"]')!;
  expect(idle.textContent).toContain('Idle');
  // Observed, so the call under "Running now" carries no note.
  expect(target.textContent).not.toContain('kernel was not observed');
});

it('shows the first line of the code without a purpose', async () => {
  serve(
    { child: [assistant([call('c1', 'python', { code: 'x = factor(N)\nprint(x)' })])] },
    observation([kernel('k-child', 'python', 'id-child')]),
  );
  const target = show();
  await settle();
  const text = target.querySelector('[data-kernel="k-child"]')!.textContent!;
  expect(text).toContain('its code, first line:');
  expect(text).toContain('x = factor(N)');
  expect(text).not.toContain('print(x)');
});

it('keeps a call whose kernel was not observed under "Running now", with a note', async () => {
  serve(
    { main: [assistant([call('c1', 'sage', { code: 'E = EllipticCurve(GF(7), [1, 2])' })])] },
    observation([kernel('k-session', 'python', null)]),
  );
  const target = show();
  await settle();
  expect(target.querySelector('.agent-card')!.textContent).toContain('sage');
  expect(target.querySelector('.agent-card')!.textContent).toContain('kernel was not observed');
  expect(target.querySelector('[data-kernel="k-session"]')!.textContent).toContain('Idle');
});
