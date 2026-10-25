// SPDX-License-Identifier: MIT
// The composer's completion (B10, item 6): `@` the session's artifacts, `#` the project's
// sessions, `/` its skills, each list read once per open and filtered as the user types;
// a choice inserts plain text, and Escape or a space closes the list.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ChatPanel from '../views/ChatPanel.svelte';
import { insertCompletion, matching, triggerAt } from './completion';

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

const artifact = (path: string, revision: number) => ({
  artifact_id: `art-${revision}`,
  revision,
  path,
  sha256: 'a'.repeat(64),
  size: 10,
  media_type: 'text/csv',
  created_at: '2026-10-25T10:00:00Z',
  result: true,
  deleted: false,
  content_retained: true,
  content_note: null,
  kind: 'table',
  session_id: 's9',
  run_id: 'run-1',
  role: 'main',
  tool_call: 'c1',
});

/** The chat's routes, and the three lists, counting each list's reads. */
function stub(reads: Record<string, number>, posts: unknown[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = new URL(url, 'http://test').pathname;
      const count = (name: string) => (reads[name] = (reads[name] ?? 0) + 1);
      if (init?.method === 'POST') {
        posts.push(JSON.parse(String(init.body)));
        return new Response(JSON.stringify({}));
      }
      if (path === '/api/v0/projects/p1/artifacts') {
        count(`artifacts ${new URL(url, 'http://test').searchParams.get('session')}`);
        return new Response(
          JSON.stringify({
            artifacts: [
              artifact('out/points.csv', 3),
              artifact('out/plot.png', 2),
              { ...artifact('gone.txt', 1), deleted: true },
            ],
            next_before: null,
            viewer: false,
          }),
        );
      }
      if (path === '/api/v0/projects/p1/skills') {
        count('skills');
        return new Response(
          JSON.stringify({
            effective: [
              { name: 'prove-bound', description: 'Prove a bound.', short: null },
              { name: 'count-points', description: 'Count points.', short: 'Points' },
            ],
          }),
        );
      }
      if (path === '/api/v0/projects/p1') {
        count('project');
        return new Response(
          JSON.stringify({
            project: {
              sessions: [
                { session_id: 'early', opened_at: '2026-10-01T00:00:00Z', closed_at: 'x', runs: 1 },
                { session_id: 's9', opened_at: '2026-10-20T00:00:00Z', closed_at: null, runs: 2 },
              ],
            },
          }),
        );
      }
      if (url.includes('/messages'))
        return new Response(
          JSON.stringify({ messages: [], run: null, service: true, environment: null }),
        );
      if (url.includes('/questions'))
        return new Response(JSON.stringify({ questions: [], run: null }));
      if (url.includes('/recovery/resources'))
        return new Response(JSON.stringify({ requests: [], next_after: null }));
      return new Response(JSON.stringify({ state: 'active', events: [], runs: [] }));
    }),
  );
}

async function composer() {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(ChatPanel, { target, props: { projectId: 'p1', sessionId: 's9', closed: false } }),
  );
  await settle();
  return target;
}

/** Types `value` into the box with the caret at its end, as the browser would. */
async function type(box: HTMLTextAreaElement, value: string): Promise<void> {
  box.value = value;
  box.setSelectionRange(value.length, value.length);
  box.dispatchEvent(new Event('input', { bubbles: true }));
  await settle();
}

function key(box: HTMLTextAreaElement, name: string): void {
  box.dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true }));
  flushSync();
}

const options = (target: HTMLElement) =>
  [...target.querySelectorAll('[role="listbox"] [role="option"]')].map(
    (item) => item.querySelector('.value')!.textContent,
  );

describe('composer completion', () => {
  it("offers the session's artifacts on @, by path, and inserts the path", async () => {
    const reads: Record<string, number> = {};
    stub(reads, []);
    const target = await composer();
    const box = target.querySelector('textarea')!;
    await type(box, 'Plot @');
    const list = target.querySelector('[role="listbox"]')!;
    expect(list.getAttribute('aria-label')).toBe("This session's artifacts");
    expect(box.getAttribute('aria-controls')).toBe(list.id);
    expect(options(target)).toEqual(['out/points.csv', 'out/plot.png']);
    // Filtered as the user types, without reading the list again.
    await type(box, 'Plot @pl');
    expect(options(target)).toEqual(['out/plot.png']);
    expect(reads['artifacts s9']).toBe(1);
    expect(box.getAttribute('aria-activedescendant')).toBe(`${list.id}-0`);
    key(box, 'Enter');
    await settle();
    expect(box.value).toBe('Plot out/plot.png ');
    expect(target.querySelector('[role="listbox"]')).toBeNull();
  });

  it("offers the project's sessions on #, newest first, and moves with the arrows", async () => {
    const reads: Record<string, number> = {};
    stub(reads, []);
    const target = await composer();
    const box = target.querySelector('textarea')!;
    const before = reads['project'] ?? 0;
    await type(box, 'As in #');
    expect(options(target)).toEqual(['s9', 'early']);
    expect(reads['project']).toBe(before + 1);
    key(box, 'ArrowDown');
    expect(target.querySelector('[aria-selected="true"] .value')!.textContent).toBe('early');
    key(box, 'ArrowDown');
    expect(target.querySelector('[aria-selected="true"] .value')!.textContent).toBe('s9');
    key(box, 'ArrowUp');
    key(box, 'Enter');
    await settle();
    expect(box.value).toBe('As in #early ');
  });

  it("offers the project's skills on /, by name, and a pointer chooses one", async () => {
    const reads: Record<string, number> = {};
    stub(reads, []);
    const target = await composer();
    const box = target.querySelector('textarea')!;
    await type(box, '/co');
    expect(target.querySelector('[role="listbox"]')!.getAttribute('aria-label')).toBe(
      "The project's skills",
    );
    expect(options(target)).toEqual(['count-points']);
    target
      .querySelector('[role="option"]')!
      .dispatchEvent(new Event('pointerdown', { bubbles: true, cancelable: true }));
    await settle();
    expect(box.value).toBe('/count-points ');
    expect(reads['skills']).toBe(1);
  });

  it('closes on Escape, stays closed for that word, and reads again on the next open', async () => {
    const reads: Record<string, number> = {};
    const posts: unknown[] = [];
    stub(reads, posts);
    const target = await composer();
    const box = target.querySelector('textarea')!;
    await type(box, 'See /');
    expect(target.querySelector('[role="listbox"]')).not.toBeNull();
    key(box, 'Escape');
    expect(target.querySelector('[role="listbox"]')).toBeNull();
    expect(box.hasAttribute('aria-controls')).toBe(false);
    await type(box, 'See /pr');
    expect(target.querySelector('[role="listbox"]')).toBeNull();
    // A space ends the word; a new trigger opens and reads its list again.
    await type(box, 'See /pr /');
    expect(options(target)).toEqual(['prove-bound', 'count-points']);
    await type(box, 'See /pr / ');
    expect(target.querySelector('[role="listbox"]')).toBeNull();
    expect(reads['skills']).toBe(2);
    // Enter with the list closed is a line break, never a send.
    key(box, 'Enter');
    expect(posts).toEqual([]);
  });
});

describe('completion text', () => {
  it('finds a trigger only at the start or after white space, up to the caret', () => {
    expect(triggerAt('@out', 4)).toEqual({ kind: 'artifacts', start: 0, query: 'out' });
    expect(triggerAt('a #s', 4)).toEqual({ kind: 'sessions', start: 2, query: 's' });
    expect(triggerAt('see\n/pro', 8)).toEqual({ kind: 'skills', start: 4, query: 'pro' });
    expect(triggerAt('a/b', 3)).toBeNull();
    expect(triggerAt('me@x.org', 8)).toBeNull();
    expect(triggerAt('@out ', 5)).toBeNull();
  });

  it('keeps at most eight matches, those starting with the word first', () => {
    const many = Array.from({ length: 12 }, (_, i) => ({
      value: `x${i}a`,
      insert: `x${i}a`,
      detail: '',
    }));
    expect(matching(many, '')).toHaveLength(8);
    expect(
      matching(
        [
          { value: 'b-ab', insert: 'b-ab', detail: '' },
          { value: 'ab', insert: 'ab', detail: '' },
        ],
        'AB',
      ).map((o) => o.value),
    ).toEqual(['ab', 'b-ab']);
  });

  it('replaces the trigger and its word with the value and a space', () => {
    expect(insertCompletion('a @ou b', { start: 2 }, 5, 'out/p.csv')).toEqual({
      text: 'a out/p.csv b',
      caret: 12,
    });
    expect(insertCompletion('#s', { start: 0 }, 2, 's9')).toEqual({ text: 's9 ', caret: 3 });
  });
});
