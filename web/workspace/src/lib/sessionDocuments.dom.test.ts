// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import SessionDocuments from '../views/SessionDocuments.svelte';
import type { DocumentInfo, DocumentRevision } from './api';
import { documentDrafts } from './drafts';
const mounted: ReturnType<typeof mount>[] = [];
afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.innerHTML = '';
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function settle() {
  for (let i = 0; i < 12; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}
const info = (
  id: string,
  title: string,
  scope: 'project' | 'session',
  session = 's1',
  deleted = false,
): DocumentInfo => ({
  document_id: id,
  project_id: 'p1',
  scope,
  session_id: scope === 'project' ? null : session,
  name: `${id}.md`,
  title,
  created_at: '2026-10-17T12:00:00Z',
  created_by: { kind: 'user' },
  created_session: session,
  takes_suggestions: true,
  head: 1,
  deleted,
});
const docs = [
  info('doc-a', 'Trail notebook', 'session'),
  info('doc-b', 'Literature notes', 'project'),
  info('doc-c', 'Private other session', 'session', 's2'),
];
function revision(document: DocumentInfo): DocumentRevision {
  return {
    document,
    revision: {
      document_id: document.document_id,
      number: 1,
      created_at: document.created_at,
      author: { kind: 'user' },
      accepted_by: null,
      session_id: document.session_id,
      base: null,
      parent: null,
      reason: 'Test',
      patch_id: null,
      tombstone: false,
      normalized: {},
      warnings: [],
      parser: {},
    },
    text: 'Notebook text',
    title: document.title,
    macros: {},
    matched: true,
    blocks: [],
    removed: [],
    removed_math: [],
  };
}
async function render() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      let body: unknown;
      if (url.endsWith('/documents')) body = { documents: docs };
      else if (url.includes('/comments')) body = { threads: [], next_after: null };
      else if (url.includes('/documents/'))
        body = revision(docs.find((d) => url.endsWith(d.document_id))!);
      else if (url.includes('/uploads')) body = { uploads: [], next_after: null };
      else if (url.endsWith('/projects/p1')) body = { sessions: [] };
      else body = { artifacts: [], next_before: null, viewer: false };
      return new Response(JSON.stringify(body));
    }),
  );
  const root = document.createElement('div');
  document.body.append(root);
  mounted.push(
    mount(SessionDocuments, { target: root, props: { projectId: 'p1', sessionId: 's1' } }),
  );
  await settle();
  return root;
}
async function select(root: HTMLElement, label: string, value: string) {
  const el = [...root.querySelectorAll('label')]
    .find((el) => el.textContent?.trim().startsWith(label))!
    .querySelector('select')!;
  // happy-dom lacks option :checked support; reproduce the native select result.
  const query = el.querySelector.bind(el);
  vi.spyOn(el, 'querySelector').mockImplementation(((selector: string) =>
    selector === ':checked'
      ? ([...el.options].find((option) => option.value === value) ?? null)
      : query(selector)) as typeof el.querySelector);
  el.value = value;
  el.dispatchEvent(new Event('change', { bubbles: true }));
  await settle();
}
const rows = (root: HTMLElement) =>
  [...root.querySelectorAll('.document-card')].map((el) => el.textContent);
it('searches titles and filenames while filtering scope and sorting', async () => {
  const root = await render();
  expect(rows(root)).toHaveLength(2);
  expect(root.querySelector('.document-list')!.textContent).not.toContain('Private other session');
  await select(root, 'Scope', 'project');
  expect(rows(root)).toHaveLength(1);
  expect(rows(root)[0]).toContain('Literature notes');
  await select(root, 'Scope', 'all');
  expect(rows(root)).toHaveLength(2);
  await select(root, 'Sort', 'name');
  expect(rows(root)[0]).toContain('Literature notes');
  const search = root.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.value = 'doc-a';
  search.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(rows(root)).toHaveLength(1);
  expect(rows(root)[0]).toContain('Trail notebook');
  search.value = 'missing';
  search.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  expect(root.querySelector('.document-list')!.textContent).toContain('No documents match');
});
it('keeps document drafts across switching, closing and reopening tabs even without storage', async () => {
  const root = await render();
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new Error('storage unavailable');
  });
  root.querySelector<HTMLButtonElement>('.document-card')!.click();
  await settle();
  const firstId = root.querySelector('.document-tabs [role="tab"][aria-selected="true"]')!.id;
  [...root.querySelectorAll<HTMLButtonElement>('button')]
    .find((el) => el.textContent === 'Edit Markdown source')!
    .click();
  await settle();
  const draft = root.querySelector<HTMLTextAreaElement>('[aria-label="Markdown draft"]')!;
  draft.value = 'Unsaved $x^2$';
  draft.dispatchEvent(new Event('input', { bubbles: true }));
  await settle();
  root.querySelector<HTMLButtonElement>('#document-tab-library')!.click();
  await settle();
  root.querySelectorAll<HTMLButtonElement>('.document-card')[1]!.click();
  await settle();
  expect(root.querySelectorAll('.document-tabs [role="tab"]')).toHaveLength(3);
  root.querySelector<HTMLButtonElement>(`#${firstId}`)!.click();
  await settle();
  expect(draft.closest<HTMLElement>('[role="tabpanel"]')!.hidden).toBe(false);
  root.querySelector<HTMLButtonElement>('[aria-label="Close Trail notebook"]')!.click();
  await settle();
  expect(root.querySelector(`#${firstId}`)).toBeNull();
  expect(root.querySelector('[aria-label="Markdown draft"]')).toBe(draft);
  expect(draft.value).toBe('Unsaved $x^2$');
  root.querySelector<HTMLButtonElement>('#document-tab-library')!.click();
  await settle();
  root.querySelector<HTMLButtonElement>('.document-card')!.click();
  await settle();
  expect(root.querySelector('[aria-label="Markdown draft"]')).toBe(draft);
  const tab = root.querySelector(`#${firstId}`)!;
  tab.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true }));
  await settle();
  expect(document.activeElement?.id).toBe('document-tab-library');
  expect(root.querySelector('#document-tab-library')!.getAttribute('aria-selected')).toBe('true');
});

it('gives source editing the full document view and restores reading without losing the draft', async () => {
  documentDrafts.clear('p1:doc-a');
  const root = await render();
  root.querySelector<HTMLButtonElement>('.document-card')!.click();
  await settle();
  expect(root.querySelector('.document-reading')?.textContent).toContain('Notebook text');

  const edit = [
    ...root.querySelectorAll<HTMLButtonElement>('.document-heading-actions button'),
  ].find((button) => button.textContent === 'Edit')!;
  edit.click();
  await settle();
  expect(root.querySelector('.document-reading')).toBeNull();
  expect(root.querySelector('.document-workspace')?.classList.contains('editing')).toBe(true);

  [...root.querySelectorAll<HTMLButtonElement>('button')]
    .find((button) => button.textContent === 'Edit Markdown source')!
    .click();
  await settle();
  const draft = root.querySelector<HTMLTextAreaElement>('[aria-label="Markdown draft"]')!;
  draft.value = 'Unsaved source';
  draft.dispatchEvent(new Event('input', { bubbles: true }));
  await settle();

  const comments = [
    ...root.querySelectorAll<HTMLButtonElement>('.document-heading-actions button'),
  ].find((button) => button.textContent === 'Comments')!;
  comments.click();
  await settle();
  expect(root.querySelector('.document-reading')?.textContent).toContain('Notebook text');
  edit.click();
  await settle();
  expect(root.querySelector('[aria-label="Markdown draft"]')).toBe(draft);
  expect(draft.value).toBe('Unsaved source');
});
