// SPDX-License-Identifier: MIT
import { afterEach, expect, it, vi } from 'vitest';
import { documentAnswered, documentRequest } from './documentRequests';

afterEach(() => vi.unstubAllGlobals());
it('keeps the operation ID for an uncertain request, changing it only after acknowledgement or payload change', async () => {
  const storage = new Map<string, string>();
  vi.stubGlobal('sessionStorage', { getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value), removeItem: (key: string) => storage.delete(key) });
  const body = { action: 'source', base: 1, text: 'Unsaved insertion.', reason: 'Edit' };
  const first = await documentRequest('p', 'retry', body);
  expect(await documentRequest('p', 'retry', body)).toEqual(first);
  expect(storage.get(first.key)).toBe(first.id);
  vi.resetModules();
  const reloaded = await import('./documentRequests');
  expect(await reloaded.documentRequest('p', 'retry', body)).toEqual(first);
  const changed = await documentRequest('p', 'retry', { ...body, text: 'Changed insertion.' });
  expect(changed.id).not.toBe(first.id);
  documentAnswered(first);
  expect(storage.has(first.key)).toBe(false);
  expect((await documentRequest('p', 'retry', body)).id).not.toBe(first.id);
  documentAnswered(changed);
});
it('isolates ownership scope and retains retries when storage is unavailable', async () => {
  vi.stubGlobal('sessionStorage', { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } });
  const body = { action: 'revert', base: 3, target: 1, reason: 'Restore' };
  const first = await documentRequest('p', 'memory-retry', body);
  expect(await documentRequest('p', 'memory-retry', body)).toEqual(first);
  expect((await documentRequest('q', 'memory-retry', body)).id).not.toBe(first.id);
});
