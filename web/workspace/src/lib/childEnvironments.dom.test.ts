// SPDX-License-Identifier: MIT
// Child environments in the workspace (CE-8, CE-11), mounted: the Compute tab shows each
// subagent's own environment under its brief's title, with its kernels and their current
// cells, and one being created as such; the environment views show the kind, the owning
// delegation, the state, the hand-back and how each is kept or released. Briefs, paths and
// the worker's messages are agents' text: a hidden character shows as its code point.
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ChildEnvironments from '../views/ChildEnvironments.svelte';
import EnvironmentPanel from '../views/EnvironmentPanel.svelte';
import SessionCompute from '../views/SessionCompute.svelte';
import type { Project } from './api';
import type { ChildEnvironmentView, RunEvent, RunTree, TreeRun } from './api-types';
import type { ChildEnvironmentListing } from './childEnvironments';
import type { KernelObservation } from './kernels';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle(): Promise<void> {
  for (let i = 0; i < 16; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

const HIDDEN = '‮';
const SHOWN = '⟦U+202E⟧';
const TIME = '2026-10-27T09:00:00Z';
const READY = 'p-child-0000000000a1';
const MAKING = 'p-child-0000000000a2';

function environment(
  id: string,
  fields: Partial<ChildEnvironmentView> = {},
  task: string | null = `Factor N${HIDDEN} now\nthen report`,
): ChildEnvironmentView {
  return {
    kind: 'child',
    environment_id: id,
    delegation_id: `dlg-${id.slice(-2).padStart(24, '0')}`,
    profile: 'standard',
    phase: 'ready',
    failure: null,
    message: null,
    isolation_tier: 3,
    created_at: TIME,
    ready_by: TIME,
    ready_at: TIME,
    expires_at: TIME,
    retained_until: null,
    hand_back: null,
    delegation:
      task === null
        ? null
        : {
            delegation_id: `dlg-${id.slice(-2).padStart(24, '0')}`,
            run: 'child',
            kind: 'task',
            brief: { task },
            ended: null,
            ended_at: null,
            review: null,
            review_reason: null,
          },
    use: 'running',
    idle_release_after: null,
    ...fields,
  };
}

const listing = (environments: ChildEnvironmentView[]): ChildEnvironmentListing => ({
  project_id: 'p',
  session_id: 's',
  environments,
});

const observation = (
  environmentId: string,
  kernels: KernelObservation['kernels'],
): KernelObservation => ({
  project_id: 'p',
  session_id: 's',
  environment_id: environmentId,
  current_revision: 1,
  observed_at: TIME,
  kernels,
});

const run = (name: string, fields: Partial<TreeRun> = {}): TreeRun => ({
  name,
  run_id: `id-${name}`,
  role: 'delegated',
  parent: 'main',
  delegation: null,
  started_at: TIME,
  state: 'running',
  stopped: null,
  kept: true,
  alias: null,
  limits: null,
  tool_mix: null,
  ...fields,
});

/** Answers the reads by path; the child environments' kernels before the session's. */
function serve(answers: { listing: ChildEnvironmentListing; events?: Record<string, RunEvent[]> }) {
  const asked: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      asked.push(url);
      const events = /\/runs\/([^/]+)\/events/.exec(url);
      if (events)
        return new Response(
          JSON.stringify({ state: 'active', events: answers.events?.[events[1]!] ?? [] }),
        );
      const child = /\/child-environments\/([^/]+)\/kernels$/.exec(url);
      if (child)
        return new Response(
          JSON.stringify(
            observation(decodeURIComponent(child[1]!), [
              {
                kernel_id: 'k-child',
                language: 'python',
                generation: 1,
                run: 'id-child',
                start_revision: 1,
                current_revision: 1,
                revision_status: 'current',
              },
            ]),
          ),
        );
      if (url.endsWith('/child-environments')) return new Response(JSON.stringify(answers.listing));
      if (url.endsWith('/kernels')) return new Response(JSON.stringify(observation('e', [])));
      return new Response('{}', { status: 500 });
    }),
  );
  return asked;
}

function show<Props extends Record<string, unknown>>(
  component: Component<Props>,
  props: Props,
): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(component, { target, props }));
  return target;
}

it("shows each subagent's own environment and its kernels on the Compute tab", async () => {
  const asked = serve({
    listing: listing([
      environment(READY),
      environment(MAKING, { phase: 'creating', ready_at: null, use: 'preparing' }, null),
    ]),
    events: {
      child: [
        {
          seq: 1,
          at: '2026-10-27T09:01:00Z',
          kind: 'assistant',
          data: {
            text: '',
            tool_calls: [{ id: 'c1', name: 'python', arguments: '{"code": "factor(N)"}' }],
          },
          pins: [],
        },
      ],
    },
  });
  const tree: RunTree = {
    runs: [run('main', { role: 'main', parent: null }), run('child')],
    delegations: [],
    delegations_total: 0,
    earlier: [],
  };
  const target = show(SessionCompute, {
    projectId: 'p',
    sessionId: 's',
    tree,
    kernelScope: { projectId: 'p', sessionId: 's', environmentId: 'e' },
  });
  await settle();
  const ready = target.querySelector(`[data-environment="${READY}"]`)!;
  expect(ready.textContent).toContain(`Factor N${SHOWN} now`);
  expect(ready.textContent).not.toContain(HIDDEN);
  expect(ready.textContent).not.toContain('then report');
  expect(ready.textContent).toContain('child');
  expect(ready.textContent).toContain('Its delegated run works in it.');
  expect(ready.querySelector('section')!.getAttribute('aria-label')).toBe(
    `Kernels of Factor N${SHOWN} now`,
  );
  // Its kernel shows the child's cell, read in the child environment.
  const kernel = ready.querySelector('[data-kernel="k-child"]')!;
  expect(kernel.textContent).toContain('Running now');
  expect(kernel.textContent).toContain('factor(N)');
  expect(asked.some((url) => url.endsWith(`/child-environments/${READY}/kernels`))).toBe(true);
  expect(asked.some((url) => url.includes(`/child-environments/${MAKING}/`))).toBe(false);
  // Observed in its own environment, the call carries no "not observed" note.
  expect(target.querySelector('.agent-card')!.textContent).not.toContain('was not observed');
  const making = target.querySelector(`[data-environment="${MAKING}"]`)!;
  expect(making.textContent).toContain('being created');
  expect(making.textContent).toContain('Being created: its kernels start once it is ready.');
  expect(making.textContent).toContain('A delegation not yet recorded');
});

it('shows the owning delegation, hand-back, retention and release in the environment views', async () => {
  const left = `out/big${HIDDEN}.bin`;
  serve({
    listing: listing([
      environment('p-child-0000000000b1', {
        phase: 'retained',
        use: 'ended',
        retained_until: '2026-10-30T09:00:00Z',
        message: `its hand-back left 1 files behind; the largest ${left} (1.5 GiB)`,
        hand_back: {
          target: 'work/dlg-0000000000000000000000b1',
          complete: false,
          copied: 1,
          retained: 0,
          left: [{ path: left, reason: '1.5 GiB (1610612736 bytes) is over the 100 MiB' }],
          left_count: 1,
        },
      }),
      environment('p-child-0000000000b2', { use: 'awaiting_review' }),
      environment('p-child-0000000000b3', {
        use: 'idle',
        idle_release_after: '2026-10-28T09:00:00Z',
      }),
    ]),
  });
  const target = show(ChildEnvironments, { projectId: 'p', sessionId: 's' });
  await settle();
  const retained = target.querySelector('[data-environment="p-child-0000000000b1"]')!;
  const text = retained.textContent!.replace(/\s+/g, ' ');
  expect(text).toContain('stopped and retained');
  expect(text).toContain('Retained until');
  expect(text).toContain('delegation dlg-0000000000000000000000b1');
  expect(text).toContain(
    'Hand-back incomplete: 1 copied into work/dlg-0000000000000000000000b1/, 1 left behind.',
  );
  expect(text).toContain(`out/big${SHOWN}.bin: 1.5 GiB (1610612736 bytes) is over the 100 MiB`);
  expect(text).toContain(`the largest out/big${SHOWN}.bin (1.5 GiB)`);
  expect(text).not.toContain(HIDDEN);
  // Its kernels are not read here.
  expect(retained.querySelector('[aria-label^="Kernels"]')).toBeNull();
  const awaiting = target.querySelector('[data-environment="p-child-0000000000b2"]')!;
  expect(awaiting.textContent).toContain('awaits the parent’s review');
  expect(awaiting.textContent).toContain('No hand-back yet.');
  const idle = target.querySelector('[data-environment="p-child-0000000000b3"]')!;
  expect(idle.textContent).toContain('Its task has settled; nothing uses it.');
  expect(idle.textContent).toContain("Released by the next main run's idle sweep from");
});

it("lists an open session's child environments under its environment", async () => {
  serve({ listing: listing([environment(READY)]) });
  const project: Project = {
    project_id: 'p',
    profile: 'minimal',
    policy: 'sha256:' + 'a'.repeat(64),
    created_at: TIME,
    updated_at: TIME,
    default_environment_id: 'e',
    sessions: [
      { session_id: 's', environment_id: 'e', dedicated: false, opened_at: TIME, closed_at: null, runs: 1 },
      { session_id: 'old', environment_id: 'e', dedicated: false, opened_at: TIME, closed_at: TIME, runs: 1 },
    ],
    environments: [{ environment_id: 'e', default: true, sessions: ['old', 's'] }],
    model_cost: { last_day: null, total: null, by_role: [], model_info_last_day: null, model_info_total: null },
  }; // prettier-ignore
  const target = show(EnvironmentPanel, {
    projectId: 'p',
    project,
  });
  await settle();
  const sections = [...target.querySelectorAll('section.child-environments')];
  expect(sections.map((s) => s.getAttribute('aria-label'))).toEqual([
    'Child environments of session s',
  ]);
  expect(sections[0]!.textContent).toContain(`Factor N${SHOWN} now`);
  expect(sections[0]!.textContent).toContain(READY);
});
