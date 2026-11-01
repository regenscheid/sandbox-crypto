// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import SkillsPage from '../views/SkillsPage.svelte';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 6; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

function project(revision = 2) {
  return {
    project_id: 'p1',
    observed_at: '2026-10-18T12:00:00Z',
    settings: { revision, always: [] as string[], excluded: [] as string[], overrides: {} },
    pins: [],
    bindings: [],
    effective: [
      {
        id: 'builtin/scientific-writing',
        source: 'builtin',
        name: 'scientific-writing',
        description: 'Write a self-standing research report',
        revision: 'abc123',
        short: null,
        topic: 'Writing',
        entry: false,
        short_by: null,
        topic_by: 'derived',
      },
      {
        id: 'builtin/pdf-reading',
        source: 'builtin',
        name: 'pdf-reading',
        description: 'Inspect papers and evidence',
        revision: 'def456',
        short: null,
        topic: 'Sources',
        entry: false,
        short_by: null,
        topic_by: 'derived',
      },
    ],
  };
}

/** The first skill's "Always" choice in its row. */
const always = (target: HTMLElement) =>
  [...target.querySelectorAll<HTMLButtonElement>('.skill-mode button')].find(
    (button) => button.textContent === 'Always',
  )!;

it("filters the catalog and opens a skill's details in its row", async () => {
  vi.stubGlobal('innerWidth', 1400);
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(project()))),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(SkillsPage, { target, props: { projectId: 'p1' } }));
  await settle();
  expect(target.querySelectorAll('.browser-row')).toHaveLength(2);
  const search = target.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.value = 'research report';
  search.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(target.querySelectorAll('.browser-row')).toHaveLength(1);
  // Details open inside the row, not in a pane beside the list, and close again.
  expect(target.querySelector('.browser-detail')).toBeNull();
  const row = target.querySelector<HTMLButtonElement>('.browser-row')!;
  row.click();
  flushSync();
  expect(row.getAttribute('aria-expanded')).toBe('true');
  expect(target.querySelector('.skill-item .skill-detail')?.textContent).toContain('abc123');
  row.click();
  flushSync();
  expect(target.querySelector('.skill-detail')).toBeNull();
});

it('reviews and saves a choice at the loaded settings revision', async () => {
  const writes: unknown[] = [];
  vi.stubGlobal('innerWidth', 1400);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit) => {
      if (init.method === 'POST') {
        writes.push(JSON.parse(init.body as string));
        return new Response(
          JSON.stringify({
            revision: 3,
            always: ['builtin/scientific-writing'],
            excluded: [],
            overrides: {},
          }),
        );
      }
      const found = project(writes.length ? 3 : 2);
      if (writes.length) found.settings.always = ['builtin/scientific-writing'];
      return new Response(JSON.stringify(found));
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(SkillsPage, { target, props: { projectId: 'p1' } }));
  await settle();
  // Each row has its own choice: On request, Always, Off.
  always(target).click();
  flushSync();
  target.querySelector<HTMLButtonElement>('[role="tab"]:nth-child(3)')!.click();
  flushSync();
  expect(target.textContent).toContain('Review pending changes');
  const save = [...target.querySelectorAll<HTMLButtonElement>('button')].find((button) =>
    button.textContent?.includes('Save at revision'),
  )!;
  save.click();
  await settle();
  expect(writes).toEqual([{ expected: 2, always: ['builtin/scientific-writing'], excluded: [] }]);
  expect(target.textContent).toContain('No pending changes');
});

it('warns only for unsaved skill changes when browser storage is full', async () => {
  vi.stubGlobal('sessionStorage', {
    getItem: () => null,
    setItem: () => {
      throw new DOMException('full', 'QuotaExceededError');
    },
    removeItem: () => {},
  });
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(project()))),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(SkillsPage, { target, props: { projectId: 'skills-storage-full' } }));
  await settle();
  expect(target.textContent).not.toContain('kept only in this tab');
  always(target).click();
  flushSync();
  expect(target.textContent).toContain('These skill loading edits are kept only in this tab');
});
