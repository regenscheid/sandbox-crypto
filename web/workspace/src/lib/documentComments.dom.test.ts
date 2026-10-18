// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import DocumentComments from '../views/DocumentComments.svelte';
import type { DocumentRevision } from './api';
import { commentDrafts } from './drafts';
import type { ReaderAnchor } from './documentSelection';

const scope = 'p1:doc1';
const revision = (number: number, source: string) =>
  ({
    document: { document_id: 'doc1', session_id: 's1', head: 5 },
    revision: { number, tombstone: false },
    blocks: [{ id: 'b1', kind: 'paragraph', source, math: [] }],
    matched: true,
    macros: {},
  }) as unknown as DocumentRevision;
const old = revision(4, 'Alpha only');
const current = revision(5, 'Rewritten only');
const anchor: ReaderAnchor = {
  documentId: 'doc1',
  revision: 4,
  block: 'b1',
  kind: 'range',
  math: '',
  quote: 'Alpha',
  note: '',
};
let component: ReturnType<typeof mount> | null = null;

async function settle() {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.replaceChildren();
  commentDrafts.clear(scope);
  vi.unstubAllGlobals();
});

it('keeps a selected comment on its original revision through a newer display and post', async () => {
  const posted: Record<string, unknown>[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        posted.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return new Response(JSON.stringify({ outcome: 'created' }));
      }
      if (url.includes('?revision=4') && !url.includes('/comments'))
        return new Response(JSON.stringify(old));
      if (url.includes('/comments'))
        return new Response(JSON.stringify({ threads: [], next_after: null }));
      return new Response(
        JSON.stringify({ project: { sessions: [{ session_id: 's1', closed_at: null }] } }),
      );
    }),
  );
  const root = document.createElement('div');
  document.body.append(root);
  let consumed = 0;
  component = mount(DocumentComments, {
    target: root,
    props: {
      projectId: 'p1',
      found: old,
      requestedAnchor: anchor,
      consumeAnchor: () => consumed++,
    },
  });
  await settle();
  const body = root.querySelector<HTMLTextAreaElement>('[aria-label="Comment Markdown"]')!;
  body.value = 'Please check this claim.';
  body.dispatchEvent(new Event('input', { bubbles: true }));
  const session = root.querySelector<HTMLSelectElement>(
    'select[aria-label="Send work request to"]',
  )!;
  expect([...session.options].map((option) => option.value)).toContain('s1');
  // happy-dom does not resolve option :checked as a native browser does.
  const query = session.querySelector.bind(session);
  vi.spyOn(session, 'querySelector').mockImplementation(((selector: string) =>
    selector === ':checked' ? (session.options[1] ?? null) : query(selector)) as typeof query);
  session.value = 's1';
  session.dispatchEvent(new Event('change', { bubbles: true }));
  await settle();
  expect(session.value).toBe('s1');
  expect(root.textContent).toContain('Post and send request');
  expect(commentDrafts.load(scope).draft?.base).toBe(4);
  expect(commentDrafts.load(scope).draft?.session).toBe('s1');
  expect(consumed).toBe(1);

  await unmount(component);
  component = mount(DocumentComments, {
    target: root,
    // A stale parent request is deliberately passed again: it must not move the saved draft.
    props: {
      projectId: 'p1',
      found: current,
      requestedAnchor: anchor,
      consumeAnchor: () => consumed++,
    },
  });
  await settle();
  expect(commentDrafts.load(scope).draft?.base).toBe(4);
  expect(root.textContent).toContain('Revision 4 · selected text');
  root.querySelector<HTMLButtonElement>('button:not([disabled])')?.focus();
  [...root.querySelectorAll<HTMLButtonElement>('button')]
    .find((button) => button.textContent?.includes('Post and send request'))!
    .click();
  await settle();
  expect(posted).toHaveLength(1);
  expect(posted[0]?.anchor).toEqual({ revision: 4, block: 'b1', start: 0, end: 5 });
  expect(posted[0]?.addressed_to).toBe('s1');
});
