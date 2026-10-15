// SPDX-License-Identifier: MIT
// The frontier view, mounted: its groups a page at a time, each once; older and newer
// windows of candidates; a statement rendered only once opened; and a failed read.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import FrontierView from '../views/FrontierView.svelte';
import type {
  FrontierAnswer,
  FrontierGroup,
  FrontierResult,
  FrontierView as View,
} from './api-types';

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

function show(): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(FrontierView, { target, props: { projectId: 'p1', category: 'key-recovery' } }),
  );
  flushSync();
  return target;
}

const result = (handle: string, fields: Partial<FrontierResult> = {}): FrontierResult => ({
  entry_id: `entry-${handle}`,
  handle,
  revision: 1,
  title: `Result ${handle}`,
  statement: `The statement of ${handle}.`,
  status: 'established',
  origin: 'project',
  disputed: false,
  unverified: false,
  author: { kind: 'user', user: 'owner' },
  conditions: {},
  measures: { time: 100 },
  evidence_types: [],
  sources: [],
  artifacts: [],
  open_findings: 0,
  open_flags: 0,
  dominated_by: [],
  linked_by: [],
  would_dominate: [],
  ...fields,
});

const group = (attack: string, fields: Partial<FrontierGroup> = {}): FrontierGroup => ({
  conditions: { attack },
  best: [result(`R-${attack.length}`)],
  dominated: [],
  not_compared: [],
  candidates: [],
  more: {},
  ...fields,
});

function answer(
  groups: FrontierGroup[],
  next: number | null,
  fields: Partial<View> = {},
): FrontierAnswer {
  return {
    project_id: 'p1',
    observed_at: 'now',
    next_groups_after: next,
    frontier: {
      category: {
        category: 'key-recovery',
        revision: 1,
        head: 1,
        target: 'Kyber-768',
        kind: 'key recovery cost',
        measures: [{ name: 'time', unit: 'bits', direction: 'lower' }],
        conditions: ['attack'],
        author: { kind: 'user', user: 'owner' },
        reason: 'compare attacks',
        operation_id: 'op-1',
        created: '2026-10-15T00:00:00Z',
        replayed: false,
      },
      groups,
      unplaced: [],
      truncated: false,
      next_candidates_before: null,
      lookalike: [],
      void_links: [],
      agent_revised: null,
      unplaced_more: 0,
      ...fields,
    },
  };
}

/** A server that answers each frontier read by its query, and records the queries. */
function server(answers: Record<string, FrontierAnswer | number>): string[] {
  const queries: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const query = new URL(url, 'http://localhost').search;
      queries.push(query);
      const found = answers[query];
      if (typeof found === 'number' || found === undefined) {
        const error = {
          error: { kind: 'unavailable', message: 'the frontier cannot be read now' },
        };
        return new Response(JSON.stringify(error), { status: found ?? 404 });
      }
      return new Response(JSON.stringify(found));
    }),
  );
  return queries;
}

const headings = (root: HTMLElement) =>
  [...root.querySelectorAll('.frontier-group h2')].map((h) => h.textContent);
const button = (root: HTMLElement, text: string) =>
  [...root.querySelectorAll('button')].find((b) => b.textContent === text);

describe('the frontier view', () => {
  it('reads its groups a page at a time, each group once', async () => {
    const queries = server({
      '': answer([group('primal')], 1),
      '?groups_after=1': answer([group('primal'), group('dual')], null),
    });
    const root = show();
    await settle();
    expect(root.textContent).toContain('Kyber-768');
    expect(root.textContent).toContain('time, in bits, lower is better');
    expect(headings(root)).toEqual(['attack: primal']);
    button(root, 'More groups')!.click();
    await settle();
    expect(queries).toEqual(['', '?groups_after=1']);
    expect(headings(root)).toEqual(['attack: primal', 'attack: dual']);
    expect(button(root, 'More groups')).toBeUndefined();
  });

  it('reads older candidates, window by window, and back to the newest', async () => {
    const window = (handle: string) =>
      group('primal', { candidates: [result(handle, { status: 'proposed' })] });
    const queries = server({
      '': answer([window('R-50')], null, { truncated: true, next_candidates_before: 40 }),
      '?candidates_before=40': answer([window('R-30')], null, {
        truncated: true,
        next_candidates_before: 12,
      }),
      '?candidates_before=12': answer([window('R-7')], null),
    });
    const root = show();
    await settle();
    expect(root.textContent).toContain('the 500 newest are read');
    expect(root.textContent).toContain('R-50');
    button(root, 'Older candidates')!.click();
    await settle();
    expect(root.textContent).toContain('made before R-40, the 500 newest of them');
    button(root, 'Older candidates')!.click();
    await settle();
    expect(root.textContent).toContain('R-7');
    expect(root.textContent).not.toContain('R-30');
    expect(button(root, 'Older candidates')).toBeUndefined();
    button(root, 'Newer candidates')!.click();
    await settle();
    expect(root.textContent).toContain('R-30');
    button(root, 'Newer candidates')!.click();
    await settle();
    expect(queries).toEqual([
      '',
      '?candidates_before=40',
      '?candidates_before=12',
      '?candidates_before=40',
      '',
    ]);
    expect(root.textContent).toContain('R-50');
    expect(button(root, 'Newer candidates')).toBeUndefined();
  });

  it('renders a statement only once it is opened', async () => {
    server({ '': answer([group('primal')], null) });
    const root = show();
    await settle();
    expect(root.textContent).not.toContain('The statement of R-6.');
    const details = root.querySelector('details')!;
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    await settle();
    expect(root.textContent).toContain('The statement of R-6.');
  });

  it('shows a failed read, and reads it again on request', async () => {
    const answers: Record<string, FrontierAnswer | number> = { '': 503 };
    const queries = server(answers);
    const root = show();
    await settle();
    expect(root.textContent).toContain('the frontier cannot be read now');
    expect(root.textContent).not.toContain('No result of this category');
    answers[''] = answer([group('primal')], null);
    button(root, 'Try again')!.click();
    await settle();
    expect(queries).toEqual(['', '']);
    expect(headings(root)).toEqual(['attack: primal']);
    expect(button(root, 'Try again')).toBeUndefined();
  });

  it('pages groups within a window of older candidates, and notes that pages may shift', async () => {
    const answers: Record<string, FrontierAnswer | number> = {
      '': answer([group('primal')], 1, { truncated: true, next_candidates_before: 40 }),
      '?candidates_before=40': answer([group('primal')], 1),
      '?groups_after=1&candidates_before=40': 503,
    };
    const queries = server(answers);
    const root = show();
    await settle();
    button(root, 'Older candidates')!.click();
    await settle();
    expect(root.textContent).not.toContain('Groups are read by their place');
    button(root, 'More groups')!.click();
    await settle();
    // A failed page leaves the way back and a retry, not an empty page.
    expect(root.textContent).toContain('the frontier cannot be read now');
    expect(button(root, 'Newer candidates')).toBeDefined();
    answers['?groups_after=1&candidates_before=40'] = answer([group('dual')], null);
    button(root, 'Try again')!.click();
    await settle();
    expect(queries).toEqual([
      '',
      '?candidates_before=40',
      '?groups_after=1&candidates_before=40',
      '?groups_after=1&candidates_before=40',
    ]);
    expect(headings(root)).toEqual(['attack: primal', 'attack: dual']);
    expect(root.textContent).toContain('Groups are read by their place');
  });

  it('keeps the way back when a window of candidates fails to read', async () => {
    server({
      '': answer([group('primal')], null, { truncated: true, next_candidates_before: 40 }),
      '?candidates_before=40': 503,
    });
    const root = show();
    await settle();
    button(root, 'Older candidates')!.click();
    await settle();
    expect(root.textContent).toContain('the frontier cannot be read now');
    button(root, 'Newer candidates')!.click();
    await settle();
    expect(headings(root)).toEqual(['attack: primal']);
  });
});
