// SPDX-License-Identifier: MIT
import { afterEach, expect, it, vi } from 'vitest';
import { flushUploads, guardPendingUploads, uploadDraft } from './uploadDrafts.svelte';
import {
  attachmentDrafts,
  attachmentMessage,
  MAX_UPLOAD_BYTES,
  uploadFile,
  type UserUpload,
} from './uploads';
const file: UserUpload = {
  upload_id: 'upload-test',
  project: 'p1',
  session: 's1',
  filename: 'équation-𝛼.md',
  media_type: 'text/markdown',
  sha256: 'a'.repeat(64),
  size: 3,
  created_at: '2026-10-17T12:00:00Z',
  author: 'user',
  path: 'shared/uploads/s1/upload-test/source.md',
  number: 1,
};
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  sessionStorage.clear();
});
it('represents a Unicode attachment manifest exactly as the server does', () => {
  const text = attachmentMessage('Check $x$  \n', [file]);
  expect(text).toContain('Check $x$\n\nAttached files');
  expect(text).toContain('"filename": "équation-𝛼.md"');
  expect(text.endsWith('\n]\n```')).toBe(true);
  expect(new TextEncoder().encode(text).length).toBe(312);
});
it('refuses an oversized file before reading bytes or making a request', async () => {
  const read = vi.fn();
  const fetch = vi.fn();
  vi.stubGlobal('fetch', fetch);
  await expect(
    uploadFile('p1', 's1', 'id', {
      size: MAX_UPLOAD_BYTES + 1,
      name: 'large.pdf',
      arrayBuffer: read,
    } as unknown as File),
  ).rejects.toThrow('8 MiB');
  expect(read).not.toHaveBeenCalled();
  expect(fetch).not.toHaveBeenCalled();
});
it('retains queued bytes and ID after failure and persists only metadata after retry', async () => {
  const draft = uploadDraft('p1', 'retry');
  const original = new File(['abc'], 'notes.md');
  draft.pending = [{ file: original, id: 'stable-upload' }];
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new Error('disconnected'))
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ ...file, session: 'retry', upload_id: 'stable-upload' })),
    );
  vi.stubGlobal('fetch', fetch);
  await flushUploads('p1', 'retry');
  expect(draft.busy).toBe(false);
  expect(draft.pending[0]!.file).toBe(original);
  expect(uploadDraft('p1', 'retry')).toBe(draft);
  const warning = new Event('beforeunload', { cancelable: true });
  guardPendingUploads(warning as BeforeUnloadEvent);
  expect(warning.defaultPrevented).toBe(true);
  await flushUploads('p1', 'retry');
  expect(draft.pending).toHaveLength(0);
  expect(draft.files).toHaveLength(1);
  expect(fetch.mock.calls.map((call) => JSON.parse(call[1].body).upload_id)).toEqual([
    'stable-upload',
    'stable-upload',
  ]);
  expect(attachmentDrafts.load('p1:retry').draft).toEqual(draft.files);
  expect(JSON.stringify(sessionStorage)).not.toContain('content_b64');
  const done = new Event('beforeunload', { cancelable: true });
  guardPendingUploads(done as BeforeUnloadEvent);
  expect(done.defaultPrevented).toBe(false);
});
it('a stalled upload releases its queue at the deadline while retaining its ID', async () => {
  vi.useFakeTimers();
  const draft = uploadDraft('p1', 'deadline');
  draft.pending = [{ file: new File(['abc'], 'notes.md'), id: 'deadline-id' }];
  vi.stubGlobal(
    'fetch',
    vi.fn(
      (_url, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal!.addEventListener('abort', () => reject(new Error('aborted')), {
            once: true,
          });
        }),
    ),
  );
  const pending = flushUploads('p1', 'deadline');
  await vi.advanceTimersByTimeAsync(60_001);
  await pending;
  expect(draft.busy).toBe(false);
  expect(draft.error).toBeTruthy();
  expect(draft.pending[0]!.id).toBe('deadline-id');
  draft.pending = [];
});
