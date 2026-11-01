// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import MemoryPage from '../views/MemoryPage.svelte';
import MemoryEntryItem from '../views/MemoryEntryItem.svelte';
import { browserState, memoryEdits } from './browserState';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(async () => {
  for (const item of mounted.splice(0)) await unmount(item);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});
async function settle() {
  for (let i = 0; i < 5; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}
const entry = {
  entry_id: 'mem-1',
  revision: 2,
  text: 'Original finite-field notation.',
  changed_at: '2026-10-18T12:00:00Z',
  basis: 'user' as const,
  accepted: false,
  recorded_at: '2026-10-18T12:00:00Z',
  scope: 'project' as const,
  anchors: [],
};

it.each(['SecurityError', 'QuotaExceededError'])(
  'warns about memory-only edits and new entries when storage raises %s',
  async (kind) => {
    vi.stubGlobal('sessionStorage', {
      getItem: () => {
        if (kind === 'SecurityError') throw new DOMException('blocked', kind);
        return null;
      },
      setItem: () => {
        throw new DOMException('unavailable', kind);
      },
      removeItem: () => {},
    });
    const projectId = `storage-${kind}`;
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (url: string) =>
          new Response(
            JSON.stringify(
              url.includes('/proposals')
                ? { proposals: [] }
                : {
                    memory: {
                      project: projectId,
                      version: 1,
                      entries: [],
                      characters: 0,
                      given: 0,
                      waiting: 0,
                    },
                    bound: 16000,
                  },
            ),
          ),
      ),
    );
    const page = document.createElement('div');
    const target = document.createElement('div');
    document.body.append(page, target);
    mounted.push(mount(MemoryPage, { target: page, props: { projectId } }));
    await settle();
    expect(page.textContent).not.toContain('kept only in this tab');
    const adding = page.querySelector<HTMLTextAreaElement>('[aria-label="A new entry"]')!;
    adding.value = 'An unsaved new memory entry.';
    adding.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(page.textContent).toContain('This new memory entry is kept only in this tab');

    const props = {
      projectId,
      entry,
      first: true,
      last: true,
      given: true,
      busy: false,
      onchange: vi.fn(async () => false),
    };
    let component = mount(MemoryEntryItem, { target, props });
    [...target.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent === 'Edit')!
      .click();
    flushSync();
    const draft = target.querySelector('textarea')!;
    draft.value = 'An unsaved correction.';
    draft.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(target.textContent).toContain('This memory edit is kept only in this tab');
    await unmount(component);
    target.innerHTML = '';
    component = mount(MemoryEntryItem, { target, props });
    mounted.push(component);
    await settle();
    expect(target.querySelector('textarea')?.value).toBe('An unsaved correction.');
    expect(target.textContent).toContain('This memory edit is kept only in this tab');
    memoryEdits.clear(`${projectId}:mem-1`);
    browserState.clear(`memory:${projectId}`);
  },
);

it('reports damaged saved memory edits without clearing them on mount', async () => {
  const scope = 'damaged-memory:mem-1';
  memoryEdits.clear(scope);
  const key = `draft:memory-entry:${scope}`;
  sessionStorage.setItem(key, '{broken');
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(MemoryEntryItem, {
      target,
      props: {
        projectId: 'damaged-memory',
        entry,
        first: true,
        last: true,
        given: true,
        busy: false,
        onchange: vi.fn(async () => false),
      },
    }),
  );
  await settle();
  expect(target.textContent).toContain('A saved memory edit could not be restored');
  expect(sessionStorage.getItem(key)).toBe('{broken');
  memoryEdits.clear(scope);
});
it('retains memory edit text and original revision across navigation and conflict', async () => {
  const scope = 'edit-browser:mem-1';
  memoryEdits.clear(scope);
  const target = document.createElement('div');
  document.body.append(target);
  const onchange = vi.fn(async () => false);
  let component = mount(MemoryEntryItem, {
    target,
    props: {
      projectId: 'edit-browser',
      entry,
      first: true,
      last: true,
      given: true,
      busy: false,
      onchange,
    },
  });
  [...target.querySelectorAll<HTMLButtonElement>('button')]
    .find((button) => button.textContent === 'Edit')!
    .click();
  flushSync();
  const textarea = target.querySelector('textarea')!;
  textarea.value = 'Draft retained $x^2$';
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  await unmount(component);
  target.innerHTML = '';
  component = mount(MemoryEntryItem, {
    target,
    props: {
      projectId: 'edit-browser',
      entry: { ...entry, revision: 3, text: 'A newer accepted value.' },
      first: true,
      last: true,
      given: true,
      busy: false,
      onchange,
    },
  });
  mounted.push(component);
  await settle();
  expect(target.querySelector('textarea')?.value).toBe('Draft retained $x^2$');
  expect(target.textContent).toContain('based on revision 2');
  expect(target.textContent).toContain('A newer accepted value.');
  [...target.querySelectorAll<HTMLButtonElement>('button')]
    .find((button) => button.textContent === 'Save')!
    .click();
  await settle();
  expect(onchange).toHaveBeenCalledWith({
    action: 'edit',
    entry: 'mem-1',
    expected: 2,
    text: 'Draft retained $x^2$',
  });
  memoryEdits.clear(scope);
});
it('filters memory without changing receive order and retains selection on remount', async () => {
  browserState.clear('memory:memory-browser');
  const entries = [entry, { ...entry, entry_id: 'mem-2', text: 'Second accepted note.' }];
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async (url: string) =>
        new Response(
          JSON.stringify(
            url.includes('/proposals')
              ? { proposals: [] }
              : {
                  memory: {
                    project: 'memory-browser',
                    version: 1,
                    entries,
                    characters: 60,
                    given: 1,
                    waiting: 0,
                  },
                  bound: 16000,
                },
          ),
        ),
    ),
  );
  const target = document.createElement('div');
  document.body.append(target);
  let component = mount(MemoryPage, { target, props: { projectId: 'memory-browser' } });
  await settle();
  const rows = () => [...target.querySelectorAll<HTMLElement>('.memory-list > li')];
  // Each entry in the order agents receive it, numbered by that order.
  expect(rows().map((row) => row.querySelector('.entry-number')!.textContent)).toEqual(['1', '2']);
  const search = target.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.value = 'Second';
  search.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  const visibleRows = () => rows().filter((row) => !row.hidden);
  expect(visibleRows().map((row) => row.querySelector('.entry-number')!.textContent)).toEqual([
    '2',
  ]);
  visibleRows()[0]!.querySelector<HTMLButtonElement>('.memory-row')!.click();
  flushSync();
  expect(visibleRows()[0]!.querySelector('.entry-detail')?.textContent).toContain('mem-2');
  await unmount(component);
  target.innerHTML = '';
  component = mount(MemoryPage, { target, props: { projectId: 'memory-browser' } });
  mounted.push(component);
  await settle();
  expect(target.querySelector<HTMLInputElement>('input[type="search"]')?.value).toBe('Second');
  expect(visibleRows()[0]!.querySelector('.memory-row')?.getAttribute('aria-expanded')).toBe(
    'true',
  );
  const cleared = target.querySelector<HTMLInputElement>('input[type="search"]')!;
  cleared.value = '';
  cleared.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(target.textContent).toContain('Past agent budget');
  browserState.clear('memory:memory-browser');
});
