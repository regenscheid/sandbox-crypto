// SPDX-License-Identifier: MIT
import { afterEach, describe, expect, it, vi } from 'vitest';
import { clearDraft, loadDraft, saveDraft } from './drafts';

afterEach(() => vi.unstubAllGlobals());
const draft = { base: 2, text: 'Unsaved $x$.', reason: 'Edit', block: null, retained: '' };

describe('document drafts', () => {
  it('retains text through view changes even when browser storage fails', () => {
    vi.stubGlobal('sessionStorage', { setItem() { throw new Error('quota'); }, getItem() { return null; }, removeItem() {} });
    expect(saveDraft('p', 'd-memory', draft)).toBe(false);
    expect(loadDraft('p', 'd-memory')).toEqual(draft);
    clearDraft('p', 'd-memory');
    expect(loadDraft('p', 'd-memory')).toBeNull();
  });
  it('restores tab drafts with their base and retained resolution text', () => {
    vi.stubGlobal('sessionStorage', { getItem: () => JSON.stringify({ ...draft, retained: 'Before resolution' }) });
    expect(loadDraft('p', 'd-storage')).toEqual({ ...draft, retained: 'Before resolution' });
  });
  it('does not load malformed or oversized browser data', () => {
    vi.stubGlobal('sessionStorage', { getItem: () => JSON.stringify({ ...draft, base: -1 }) });
    expect(loadDraft('p', 'd-invalid')).toBeNull();
    vi.stubGlobal('sessionStorage', { getItem: () => 'x'.repeat(4 * 1024 * 1024) });
    expect(loadDraft('p', 'd-huge')).toBeNull();
  });
  it('isolates documents and projects', () => {
    vi.stubGlobal('sessionStorage', { setItem() {}, getItem() { return null; }, removeItem() {} });
    saveDraft('p', 'd-isolated', draft);
    expect(loadDraft('q', 'd-isolated')).toBeNull();
    expect(loadDraft('p', 'd-other')).toBeNull();
    clearDraft('p', 'd-isolated');
  });
});
