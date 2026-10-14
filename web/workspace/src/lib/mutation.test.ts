// SPDX-License-Identifier: MIT
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

interface Note {
  base: number;
  text: string;
}
const validNote = (value: unknown): value is Note =>
  typeof value === 'object' &&
  value !== null &&
  Number.isSafeInteger((value as Note).base) &&
  typeof (value as Note).text === 'string';

let storage: Map<string, string>;
beforeEach(() => {
  vi.resetModules();
  storage = new Map();
  vi.stubGlobal('sessionStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => storage.set(k, v),
    removeItem: (k: string) => storage.delete(k),
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
});
// The modules are loaded afresh per test (a reload), so ApiError comes from the same load.
const fresh = async () => ({ ...(await import('./mutation')), ...(await import('./http')) });

describe('typed drafts', () => {
  it('survive a reload, by kind and scope, and are cleared', async () => {
    const { DraftStore } = await fresh();
    const notes = new DraftStore<Note>('note', validNote);
    expect(notes.save('p:a', { base: 2, text: 'hello' })).toBe(true);
    vi.resetModules();
    const reloaded = new (await fresh()).DraftStore<Note>('note', validNote);
    expect(reloaded.load('p:a')).toEqual({
      draft: { base: 2, text: 'hello' },
      damaged: false,
      stored: true,
    });
    expect(reloaded.load('p:b').draft).toBeNull();
    expect(new (await fresh()).DraftStore<Note>('other', validNote).load('p:a').draft).toBeNull();
    expect(reloaded.clear('p:a')).toBe(true);
    expect(reloaded.load('p:a').draft).toBeNull();
    expect(storage.size).toBe(0);
  });
  it('refuse an invalid draft, and hold a damaged stored one untouched', async () => {
    const { DraftStore } = await fresh();
    const notes = new DraftStore<Note>('note', validNote);
    expect(notes.save('p:a', { base: 'x' } as unknown as Note)).toBe(false);
    storage.set('draft:note:p:a', '{broken');
    expect(notes.load('p:a')).toEqual({ draft: null, damaged: true, stored: false });
    storage.set('draft:note:p:a', JSON.stringify({ format: 1, scope: 'p:a', draft: { base: 1 } }));
    expect(notes.load('p:a').damaged).toBe(true);
    expect(storage.get('draft:note:p:a')).toContain('"base":1');
  });
  it('keep a draft in memory when the tab cannot store it, and say so', async () => {
    vi.stubGlobal('sessionStorage', {
      getItem: () => null,
      setItem: () => {
        throw new Error('quota');
      },
      removeItem: () => undefined,
    });
    const { DraftStore } = await fresh();
    const notes = new DraftStore<Note>('note', validNote, 1024);
    expect(notes.save('p:a', { base: 1, text: 'kept' })).toBe(false);
    expect(notes.load('p:a')).toEqual({
      draft: { base: 1, text: 'kept' },
      damaged: false,
      stored: false,
    });
  });
});

describe('guarded writes', () => {
  it('reuse the operation ID after an unknown outcome, and forget it after an answer', async () => {
    const { guardedWrite, ApiError } = await fresh();
    const sent: string[] = [];
    const send = vi.fn(async (change: { operation_id: string }) => {
      sent.push(change.operation_id);
      if (sent.length === 1) throw new ApiError(0, 'unavailable', 'no answer');
      if (sent.length === 2) throw new Reloaded(503, 'unavailable', 'down');
      return 'saved';
    });
    const body = { base: 1, text: 'x' };
    await expect(guardedWrite('note', 'p:a', body, send)).rejects.toMatchObject({ status: 0 });
    // The ID is stored before the request, so a reload keeps it too.
    expect([...storage.values()]).toEqual([sent[0]]);
    vi.resetModules();
    const reloaded = (await fresh()).guardedWrite;
    // The reload's own ApiError, for the later answers.
    const { ApiError: Reloaded } = await import('./http');
    await expect(reloaded('note', 'p:a', body, send)).rejects.toMatchObject({ status: 503 });
    expect(await reloaded('note', 'p:a', body, send)).toBe('saved');
    expect(new Set(sent).size).toBe(1);
    expect(storage.size).toBe(0);
    await reloaded('note', 'p:a', body, send);
    expect(sent[3]).not.toBe(sent[0]);
  });
  it.each([409, 400])('forget the ID after a definitive %i refusal', async (status) => {
    const { guardedWrite, ApiError } = await fresh();
    const sent: string[] = [];
    const send = async (change: { operation_id: string }) => {
      sent.push(change.operation_id);
      throw new ApiError(status, 'conflict', 'stale');
    };
    await expect(guardedWrite('note', 'p:a', { base: 1 }, send)).rejects.toMatchObject({ status });
    await expect(guardedWrite('note', 'p:a', { base: 1 }, send)).rejects.toMatchObject({ status });
    expect(sent[0]).not.toBe(sent[1]);
    expect(storage.size).toBe(0);
  });
  it('give a different change, scope or kind its own ID', async () => {
    const { guardedWrite, ApiError } = await fresh();
    const sent: string[] = [];
    const send = async (change: { operation_id: string }) => {
      sent.push(change.operation_id);
      throw new ApiError(0, 'unavailable', 'no answer');
    };
    for (const [kind, scope, text] of [
      ['note', 'p:a', 'x'],
      ['note', 'p:a', 'y'],
      ['note', 'p:b', 'x'],
      ['other', 'p:a', 'x'],
    ] as const)
      await guardedWrite(kind, scope, { text }, send).catch(() => undefined);
    expect(new Set(sent).size).toBe(4);
  });
});
