// SPDX-License-Identifier: MIT
// The frontier category form, mounted and used: a new category defined from the list, a
// revision with what it changes, a draft the server would refuse, conflicts, and the
// exact summary, every hidden character as its code point (hidden.fixtures.json).
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import FrontierCategoryForm from '../views/FrontierCategoryForm.svelte';
import FrontierList from '../views/FrontierList.svelte';
import FrontierView from '../views/FrontierView.svelte';
import type { Category } from './api-types';
import fixtures from './hidden.fixtures.json';

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

function show<P extends Record<string, any>>(component: Component<P>, props: P): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(component, { target, props }));
  flushSync();
  return target;
}

const button = (root: HTMLElement, words: string) =>
  [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === words) as
    HTMLButtonElement | undefined;

/** The field whose label starts with `words`, set to `value`. */
function fill(root: HTMLElement, words: string, value: string, nth = 0): void {
  const field = [...root.querySelectorAll('label')]
    .filter((label) => label.textContent?.trim().startsWith(words))
    [nth]?.querySelector('input, textarea') as HTMLInputElement | undefined;
  if (!field) throw new Error(`no field "${words}"`);
  field.value = value;
  field.dispatchEvent(new Event('input'));
  flushSync();
}

const exact = (root: HTMLElement) =>
  [...root.querySelectorAll('pre.exact')].map((pre) => pre.textContent ?? '');

const category = (fields: Partial<Category> = {}): Category => ({
  category: 'key-recovery',
  revision: 1,
  head: 1,
  target: 'Kyber-768',
  kind: 'key recovery cost',
  measures: [{ name: 'time', unit: 'bits', direction: 'lower' }],
  conditions: ['attack'],
  author: { kind: 'agent', session_id: 's1', run_id: 'run-1', role: 'main' },
  reason: 'compare attacks',
  operation_id: 'op-1',
  created: '2026-10-15T00:00:00Z',
  replayed: false,
  ...fields,
});

interface Call {
  method: string;
  url: string;
  body: Record<string, unknown> | null;
}

/** The categories, the definition read, and the save's answer (a status for a failure). */
function server(state: { save: number | Category; now?: Category }): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
      calls.push({ method, url, body });
      const reply = (value: unknown, status = 200) =>
        new Response(JSON.stringify(value), { status });
      if (method === 'POST') {
        if (typeof state.save === 'number') {
          const revision = state.now?.revision;
          const error = { kind: 'conflict', message: 'x', details: { revision } };
          return reply({ error }, state.save);
        }
        return reply(state.save);
      }
      if (url.endsWith('/definition')) {
        return reply({ project_id: 'p1', observed_at: 'now', category: state.now });
      }
      return reply({
        project_id: 'p1',
        observed_at: 'now',
        page: { categories: [], next_after: null },
      });
    }),
  );
  return calls;
}

describe('the category form', () => {
  it('defines a category from the list, after showing exactly what will be saved', async () => {
    const calls = server({ save: category({ category: 'cost.v2' }) });
    const root = show(FrontierList, { projectId: 'p1' });
    await settle();
    button(root, 'Define a category…')!.click();
    flushSync();
    fill(root, 'Name', 'cost.v2');
    fill(root, 'Target', 'Kyber-768');
    fill(root, 'Kind of result', 'key recovery cost');
    fill(root, 'Name', 'time', 1);
    fill(root, 'Unit', 'bits');
    button(root, 'Add a condition key')!.click();
    flushSync();
    fill(root, 'Key', 'attack');
    fill(root, 'Your reason', 'compare attacks');
    button(root, 'Review…')!.click();
    flushSync();
    expect(exact(root)[0]).toBe(
      [
        'Category: cost.v2 (new)',
        'Target: Kyber-768',
        'Kind of result: key recovery cost',
        'Measures:',
        '  time, in bits, lower is better',
        'Condition keys: attack',
        'Reason: compare attacks',
      ].join('\n'),
    );
    button(root, 'Define cost.v2')!.click();
    await settle();
    const post = calls.find((call) => call.method === 'POST')!;
    expect(post.url).toBe('/api/v0/projects/p1/frontier');
    expect(post.body).toEqual({
      category: 'cost.v2',
      target: 'Kyber-768',
      kind: 'key recovery cost',
      measures: [{ name: 'time', unit: 'bits', direction: 'lower' }],
      conditions: ['attack'],
      reason: 'compare attacks',
      operation_id: expect.any(String),
    });
    expect(calls.filter((call) => call.url === '/api/v0/projects/p1/frontier')).toHaveLength(3);
    expect(root.querySelector('[role="status"]')!.textContent!.replace(/\s+/g, ' ').trim()).toBe(
      'You defined cost.v2, r1.',
    );
  });

  it('says why a draft would be refused, and sends nothing', async () => {
    const calls = server({ save: category() });
    const root = show(FrontierCategoryForm, { projectId: 'p1', done: vi.fn(), oncancel: vi.fn() });
    fill(root, 'Name', 'a b');
    button(root, 'Review…')!.click();
    flushSync();
    expect(root.textContent).toContain("The category's name is a letter or digit");
    expect(exact(root)).toEqual([]);
    expect(calls).toEqual([]);
  });

  it('revises at the revision read, saying what the revision changes', async () => {
    const saved = category({ revision: 2, head: 2 });
    const calls = server({ save: saved });
    const done = vi.fn();
    const root = show(FrontierCategoryForm, {
      projectId: 'p1',
      current: category(),
      done,
      oncancel: vi.fn(),
    });
    const higher = [...root.querySelectorAll<HTMLInputElement>('input[type="radio"]')].find(
      (input) => input.value === 'higher',
    )!;
    higher.click();
    flushSync();
    fill(root, 'Your reason', 'larger is better here');
    button(root, 'Review…')!.click();
    flushSync();
    expect(exact(root)[0]!.split('\n')[0]).toBe('Category: key-recovery, revising r1');
    expect(root.textContent).toContain("measure time's direction: higher is better");
    button(root, 'Save r2')!.click();
    await settle();
    expect(calls[0]!.body).toMatchObject({
      category: 'key-recovery',
      expected: 1,
      measures: [{ name: 'time', unit: 'bits', direction: 'higher' }],
      reason: 'larger is better here',
    });
    expect(done).toHaveBeenCalledWith(saved);
  });

  it('on a conflict saves nothing, reads the category as it is now, and reviews the draft against it', async () => {
    const now = category({ revision: 3, head: 3, target: 'Kyber-1024' });
    const state: { save: number | Category; now: Category } = { save: 409, now };
    const calls = server(state);
    const done = vi.fn();
    const root = show(FrontierCategoryForm, {
      projectId: 'p1',
      current: category(),
      done,
      oncancel: vi.fn(),
    });
    fill(root, 'Your reason', 'r');
    button(root, 'Review…')!.click();
    flushSync();
    button(root, 'Save r2')!.click();
    await settle();
    expect(root.textContent).toContain(
      'key-recovery is at r3 now, not the r1 you read: nothing was saved.',
    );
    expect(calls.at(-1)!.url).toBe('/api/v0/projects/p1/frontier/key-recovery/definition');
    expect(exact(root)[1]!.split('\n').slice(0, 2)).toEqual([
      'Category: key-recovery, r3',
      'Target: Kyber-1024',
    ]);
    expect(button(root, 'Save r2')!.disabled).toBe(true);
    expect(done).not.toHaveBeenCalled();
    button(root, 'Review my draft as a revision of r3')!.click();
    flushSync();
    // The draft's target differs from r3's: the review says so before anything is saved.
    expect(exact(root)[0]!.split('\n')[0]).toBe('Category: key-recovery, revising r3');
    expect(root.textContent).toContain('the target');
    state.save = category({ revision: 4, head: 4 });
    button(root, 'Save r4')!.click();
    await settle();
    expect(
      calls.filter((call) => call.method === 'POST').map((call) => call.body!.expected),
    ).toEqual([1, 3]);
    expect(done).toHaveBeenCalledTimes(1);
  });

  it('on defining a category that exists, offers to review the draft as a revision of it', async () => {
    server({ save: 409, now: category({ revision: 2, head: 2 }) });
    const root = show(FrontierCategoryForm, { projectId: 'p1', done: vi.fn(), oncancel: vi.fn() });
    fill(root, 'Name', 'key-recovery');
    fill(root, 'Target', 't');
    fill(root, 'Kind of result', 'k');
    fill(root, 'Name', 'time', 1);
    fill(root, 'Unit', 'bits');
    fill(root, 'Your reason', 'r');
    button(root, 'Review…')!.click();
    flushSync();
    button(root, 'Define key-recovery')!.click();
    await settle();
    expect(root.textContent).toContain('A category named key-recovery exists already, at r2');
    button(root, 'Review my draft as a revision of r2')!.click();
    flushSync();
    expect(button(root, 'Save r3')).toBeDefined();
  });

  it('revises the category from its frontier, then reads the frontier again', async () => {
    let shown = category();
    const frontierReads: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
          shown = category({ revision: 2, head: 2, reason: 'r' });
          return new Response(JSON.stringify(shown));
        }
        frontierReads.push(url);
        return new Response(
          JSON.stringify({
            project_id: 'p1',
            observed_at: 'now',
            next_groups_after: null,
            next_groups_key: null,
            frontier: {
              category: shown,
              groups: [],
              unplaced: [],
              truncated: false,
              next_candidates_before: null,
              lookalike: [],
              void_links: [],
              agent_revised: null,
              unplaced_more: 0,
            },
          }),
        );
      }),
    );
    const root = show(FrontierView, { projectId: 'p1', category: 'key-recovery' });
    await settle();
    button(root, 'Revise this category…')!.click();
    flushSync();
    fill(root, 'Your reason', 'r');
    button(root, 'Review…')!.click();
    flushSync();
    button(root, 'Save r2')!.click();
    await settle();
    expect(frontierReads).toHaveLength(2);
    expect(root.querySelector('[role="status"]')!.textContent).toBe(
      'Your revision r2 of key-recovery is saved.',
    );
    expect(root.textContent).toContain('r2, ');
    expect(button(root, 'Revise this category…')).toBeDefined();
  });

  const cases = fixtures.cases.map((c) => ({ ...c, hidden: c.hidden as [number, string][] }));
  it.each(cases)(
    "$name, in an agent's target the user revises: each hidden character as its code point",
    ({ text, hidden }) => {
      server({ save: category() });
      const root = show(FrontierCategoryForm, {
        projectId: 'p1',
        current: category({ target: text }),
        done: vi.fn(),
        oncancel: vi.fn(),
      });
      fill(root, 'Your reason', 'r');
      button(root, 'Review…')!.click();
      flushSync();
      const shown = exact(root)[0]!;
      for (const [, code] of hidden) expect(shown).toContain(`⟦${code}⟧`);
      if (hidden.length === 0) expect(shown).toContain(`Target: ${text}\n`);
    },
  );
});
