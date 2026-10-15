// SPDX-License-Identifier: MIT
// A review's delegated work and a fix's cited files, mounted in the review panel: each item
// labelled with where it comes from, a delegation's outcome read whole as the run tree
// shows a delegation, and a delegated run's actions under its delegation.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ReviewsPanel from '../views/ReviewsPanel.svelte';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

const pin = { artifact_id: 'art-1', revision: 2, sha256: 'ab'.repeat(32) };
const item = (evidence: string, kind: string, locator: Record<string, unknown>) => ({
  evidence,
  ordinal: 1,
  kind,
  locator,
  sha256: 'ab'.repeat(32),
  complete: true,
});

const items = [
  item('ev-cited', 'artifact', {
    artifact: pin,
    run: 'run-a',
    path: 'results/ell.json',
    cited_by: { finding: 'finding-1' },
  }),
  item('ev-result', 'artifact', {
    artifact: pin,
    run: 'run-c',
    path: 'out/table.csv',
    delegated: { run: 'run-c', delegation: 'del-2' },
  }),
  item('ev-outcome', 'delegation', { run: 'run-c', delegation: 'del-2', name: 'run-c' }),
  item('ev-actions', 'actions', { actions: 'window', from: 1, to: 9 }),
];

const outcome = {
  delegation: 'del-2',
  kind: 'task',
  brief: { task: 'Tabulate the costs.' },
  ended: 'returned',
  stopped: 'final',
  result: { status: 'complete', summary: 'Tabulated.' },
  read_entry: null,
  read_proofs: null,
  refused: null,
  alias: 'worker-a',
  models: [],
  report: 'Done.',
  report_clipped: false,
};

const actions = {
  actions: [
    { tool: 'delegate', target: {}, outcome: 'ok', made: ['delegation del-2'], run: 'run-a' },
    {
      tool: 'execute',
      target: { path: 'tab.py' },
      outcome: 'refused',
      made: [],
      run: 'run-c',
      delegation: 'del-2',
    },
  ],
  total: 2,
  documents: [],
};

function server(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const reply = (value: unknown) => new Response(JSON.stringify(value));
      const text = (value: unknown) =>
        reply({ text: JSON.stringify(value), next_start: null, locator: {}, complete: true });
      if (url.includes('/evidence/ev-outcome')) return text(outcome);
      if (url.includes('/evidence/ev-actions')) return text(actions);
      if (url.includes('/reviews/review-1?')) {
        return reply({
          review: {
            review_id: 'review-1',
            kind: 'confirmation',
            state: 'passed',
            summary: '',
            created: 1_792_000_000,
            alias: null,
            resolved_model: null,
            rubric: 'r',
            calls: 1,
            input_tokens: 1,
            output_tokens: 1,
            estimated_tokens: 1,
            cap_reached: false,
            coverage_complete: true,
            start_seq: 1,
            end_seq: 9,
            config_revision: 'c',
            classifier: {},
          },
          evidence: items,
          next_after: null,
        });
      }
      if (url.includes('/reviews')) return reply({ reviews: [], next_after: null, unreviewed: [] });
      if (url.includes('/math-checks')) return reply({ checks: [], next_after: null, queue: [] });
      return reply({ findings: [], next_after: null });
    }),
  );
}

describe("a review's delegated work", () => {
  it('is labelled by where it comes from, and read whole as it is', async () => {
    server();
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(
      mount(ReviewsPanel, {
        target,
        props: { projectId: 'p1', sessionId: 's1', reviewId: 'review-1' },
      }),
    );
    await settle();
    const detail = target.querySelector('[aria-label="Review detail"]')!;
    const rows = [...detail.querySelectorAll(':scope > ul > li')];
    const said = rows.map((row) => row.querySelector('.context')!.textContent);
    expect(said).toEqual([
      'Cited by the fix:',
      'Result file of delegation del-2:',
      'Delegated work, as context, not reviewed material:',
      "Derived from the window's tool records:",
    ]);
    expect(rows[0]!.textContent).toContain('file results/ell.json');
    expect(rows[0]!.querySelector('a')!.getAttribute('href')).toBe(
      '#/projects/p1/sessions/s1/findings/finding-1',
    );
    expect(rows[1]!.textContent).toContain('file out/table.csv');
    rows[2]!.querySelector('button')!.click();
    await settle();
    expect(detail.querySelector('.delegation')!.textContent).toContain('Tabulated.');
    expect(detail.querySelector('p.context')!.textContent!.trim()).toBe(
      'Delegated work, as context, not reviewed material:',
    );
    rows[3]!.querySelector('button')!.click();
    await settle();
    const section = detail.querySelector('[aria-label="What the runs did"]')!;
    const heads = [...section.querySelectorAll(':scope > p')].map((p) =>
      p.textContent!.replace(/\s+/g, ' ').trim(),
    );
    expect(heads).toEqual(['Run run-a', 'Delegated run run-c, of delegation del-2']);
    expect(section.querySelectorAll('table')[1]!.querySelector('strong.problem')!.textContent).toBe(
      'refused',
    );
  });
});
