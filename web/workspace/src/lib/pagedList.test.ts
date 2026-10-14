// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { emptyList, PagedList, type Page } from './pagedList';

/** A reader whose pages the test answers, one request at a time. */
function pages() {
  const asked: (number | null)[] = [];
  const pending: { resolve: (page: Page<string, number>) => void; reject: (e: unknown) => void }[] =
    [];
  const read = (cursor: number | null) => {
    asked.push(cursor);
    return new Promise<Page<string, number>>((resolve, reject) =>
      pending.push({ resolve, reject }),
    );
  };
  return { asked, pending, read };
}

describe('a list read a page at a time', () => {
  it('reads the first page, then appends the next by its cursor', async () => {
    const { asked, pending, read } = pages();
    const state = emptyList<string, number>();
    const list = new PagedList(state, read);
    const first = list.reload();
    expect(state.loading).toBe(true);
    pending[0]!.resolve({ items: ['a', 'b'], next: 2 });
    await first;
    expect(state).toEqual({ items: ['a', 'b'], next: 2, loading: false, error: null });
    const second = list.more();
    pending[1]!.resolve({ items: ['c'], next: null });
    await second;
    expect(asked).toEqual([null, 2]);
    expect(state.items).toEqual(['a', 'b', 'c']);
    expect(state.next).toBeNull();
    await list.more(); // complete: nothing asked
    expect(asked).toEqual([null, 2]);
  });

  it('asks for no second page while one is on its way', async () => {
    const { asked, pending, read } = pages();
    const state = emptyList<string, number>();
    const list = new PagedList(state, read);
    const first = list.reload();
    void list.more(); // the first page is on its way
    pending[0]!.resolve({ items: ['a'], next: 1 });
    await first;
    const one = list.more();
    void list.more();
    expect(asked).toEqual([null, 1]);
    pending[1]!.resolve({ items: ['b'], next: null });
    await one;
    expect(state.items).toEqual(['a', 'b']);
  });

  it('drops a page that a reload superseded, and its failure', async () => {
    const { pending, read } = pages();
    const state = emptyList<string, number>();
    const list = new PagedList(state, read);
    const first = list.reload();
    pending[0]!.resolve({ items: ['old'], next: 1 });
    await first;
    const late = list.more();
    const reload = list.reload();
    pending[2]!.resolve({ items: ['new'], next: null });
    await reload;
    pending[1]!.reject(new Error('late'));
    await late;
    expect(state).toEqual({ items: ['new'], next: null, loading: false, error: null });
  });

  it('keeps the items read before a failure, and the failure', async () => {
    const { pending, read } = pages();
    const state = emptyList<string, number>();
    const list = new PagedList(state, read);
    const first = list.reload();
    pending[0]!.resolve({ items: ['a'], next: 1 });
    await first;
    const more = list.more();
    const failure = new Error('offline');
    pending[1]!.reject(failure);
    await more;
    expect(state).toEqual({ items: ['a'], next: 1, loading: false, error: failure });
  });
});
