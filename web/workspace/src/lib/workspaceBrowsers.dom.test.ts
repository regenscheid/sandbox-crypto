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
};
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
  expect(
    [...target.querySelectorAll('.memory-row strong')].map((item) => item.textContent),
  ).toEqual(['1. mem-1', '2. mem-2']);
  const search = target.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.value = 'Second';
  search.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  target.querySelector<HTMLButtonElement>('.memory-row')!.click();
  flushSync();
  expect(target.querySelector('.memory-row strong')?.textContent).toBe('2. mem-2');
  await unmount(component);
  target.innerHTML = '';
  component = mount(MemoryPage, { target, props: { projectId: 'memory-browser' } });
  mounted.push(component);
  await settle();
  expect(target.querySelector<HTMLInputElement>('input[type="search"]')?.value).toBe('Second');
  expect(target.querySelector('.memory-row')?.getAttribute('aria-pressed')).toBe('true');
  expect(target.textContent).toContain('Past agent budget');
  browserState.clear('memory:memory-browser');
});
