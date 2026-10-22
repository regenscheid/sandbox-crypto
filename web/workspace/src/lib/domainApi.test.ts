// SPDX-License-Identifier: MIT
import { afterEach, expect, expectTypeOf, it, vi } from 'vitest';
import { api } from './api';
import type {
  CommentChangeRequest,
  DocumentChangeRequest,
  DocumentRequestReplaceText,
  JobCancellation,
} from './api-types';
import { ApiError } from './http';

afterEach(() => vi.unstubAllGlobals());

function answering(value: unknown = {}) {
  const fetch = vi.fn<typeof globalThis.fetch>(async () => new Response(JSON.stringify(value)));
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

it('keeps document paths, revision omission, signal and listing unwrapping', async () => {
  const fetch = answering({ documents: [{ name: 'notes' }] });
  const signal = new AbortController().signal;
  expect(await api.documents('a/b', signal)).toEqual([{ name: 'notes' }]);
  expect(fetch.mock.calls[0]).toMatchObject([
    '/api/v0/projects/a%2Fb/documents',
    { signal, method: 'GET', cache: 'no-store', redirect: 'error' },
  ]);
  await api.document('p', 'a b');
  await api.document('p', 'a b', 3);
  await api.history('p', 'a b');
  await api.previewPatch('p', 'a b', 'patch/x');
  expect(fetch.mock.calls.slice(1).map((call) => call[0])).toEqual([
    '/api/v0/projects/p/documents/a%20b',
    '/api/v0/projects/p/documents/a%20b?revision=3',
    '/api/v0/projects/p/documents/a%20b/history',
    '/api/v0/projects/p/documents/a%20b/patches/patch%2Fx/preview',
  ]);
});

it('keeps comment cursors and exact change bodies, including null and omitted defaults', async () => {
  const fetch = answering();
  await api.comments('p', 'd', 2);
  await api.comments('p', 'd', 2, 'a/b');
  await api.commentThread('p', 'd', 'a/b');
  await api.commentThread('p', 'd', 'a/b', 4);
  const change: CommentChangeRequest = {
    action: 'create',
    anchor: { revision: 2, block: 'b1' },
    body: 'Question',
    addressed_to: null,
    operation_id: 'comment-1',
  };
  await api.changeComment('p', 'd', change);
  expect(fetch.mock.calls.map((call) => call[0])).toEqual([
    '/api/v0/projects/p/documents/d/comments?revision=2',
    '/api/v0/projects/p/documents/d/comments?revision=2&after=a%2Fb',
    '/api/v0/projects/p/documents/d/comments/a%2Fb',
    '/api/v0/projects/p/documents/d/comments/a%2Fb?before=4',
    '/api/v0/projects/p/documents/d/comments',
  ]);
  expect(fetch.mock.calls[4]?.[1]).toMatchObject({ method: 'POST', body: JSON.stringify(change) });
});

it('keeps patch wire aliases and the empty job cancellation body', async () => {
  const fetch = answering();
  const change: DocumentChangeRequest = {
    action: 'patch',
    base: 1,
    reason: 'Correct text',
    operation_id: 'edit-1',
    operations: [{ op: 'replace_text', block: 'b1', quote: 'old', with: 'new' }],
  };
  await api.changeDocument('p', 'd', change);
  await api.jobs('p', 'a/b');
  await api.cancelJob('p', 'a/b', 'j x');
  expect(fetch.mock.calls[0]?.[1]).toMatchObject({ method: 'POST', body: JSON.stringify(change) });
  expect(fetch.mock.calls[1]?.[0]).toBe('/api/v0/projects/p/sessions/a%2Fb/jobs');
  expect(fetch.mock.calls[2]).toMatchObject([
    '/api/v0/projects/p/sessions/a%2Fb/jobs/j%20x/cancel',
    { method: 'POST', body: '{}' },
  ]);
});

it('retains shared transport errors in the public facade', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(JSON.stringify({ error: { kind: 'conflict', message: 'stale base' } }), {
          status: 409,
        }),
    ),
  );
  await expect(api.history('p', 'd')).rejects.toBeInstanceOf(ApiError);
});

it('exposes generated mutation unions and the brief cancellation response', () => {
  expectTypeOf<Parameters<typeof api.changeDocument>[2]>().toEqualTypeOf<DocumentChangeRequest>();
  expectTypeOf<Parameters<typeof api.changeComment>[2]>().toEqualTypeOf<CommentChangeRequest>();
  expectTypeOf<Awaited<ReturnType<typeof api.cancelJob>>>().toEqualTypeOf<JobCancellation>();
});

// Checked by svelte-check/tsc; never sends these requests.
function requestTypes() {
  const edit: DocumentChangeRequest = {
    action: 'patch',
    base: 1,
    reason: 'Edit',
    operation_id: 'o1',
    operations: [{ op: 'insert', markdown: 'New', before: 'b1' }],
  };
  const comment: CommentChangeRequest = {
    action: 'reply',
    thread: 'ann-id',
    expected: 1,
    body: 'Reply',
    operation_id: 'o2',
  };
  void [edit, comment];
  // @ts-expect-error A source edit needs text.
  const incomplete: DocumentChangeRequest = {
    action: 'source',
    base: 1,
    reason: 'Edit',
    operation_id: 'o1',
  };
  // @ts-expect-error An action cannot silently become an arbitrary mutation.
  const unknown: CommentChangeRequest = { action: 'delete', thread: 'ann-id', operation_id: 'o1' };
  const alias: DocumentRequestReplaceText = {
    op: 'replace_text',
    block: 'b1',
    quote: 'old',
    // @ts-expect-error The JSON property is "with", not the Python attribute "with_".
    with_: 'new',
  };
  void [incomplete, unknown, alias];
}
void requestTypes;
