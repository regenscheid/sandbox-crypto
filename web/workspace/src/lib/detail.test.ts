// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { Detail, noDetail } from './detail';

/** A reader whose answers the test gives, one request at a time. */
function reads() {
  const asked: string[] = [];
  const pending: { resolve: (value: string) => void; reject: (e: unknown) => void }[] = [];
  const read = (key: string) => {
    asked.push(key);
    return new Promise<string>((resolve, reject) => pending.push({ resolve, reject }));
  };
  return { asked, pending, read };
}

describe('a detail shown by its key', () => {
  it('shows the value read for the key', async () => {
    const { pending, read } = reads();
    const state = noDetail<string, string>();
    const detail = new Detail(state, read);
    const shown = detail.show('a');
    expect(state).toEqual({ key: 'a', value: null, loading: true, error: null });
    pending[0]!.resolve('A');
    await shown;
    expect(state).toEqual({ key: 'a', value: 'A', loading: false, error: null });
  });

  it('keeps the value while the same key is read again, and clears another key at once', async () => {
    const { pending, read } = reads();
    const state = noDetail<string, string>();
    const detail = new Detail(state, read);
    const first = detail.show('a');
    pending[0]!.resolve('A');
    await first;
    const again = detail.reload();
    expect(state.value).toBe('A');
    pending[1]!.resolve('A2');
    await again;
    expect(state.value).toBe('A2');
    void detail.show('b');
    expect(state).toMatchObject({ key: 'b', value: null, loading: true });
  });

  it('drops the answer for a key chosen earlier, and its failure', async () => {
    const { pending, read } = reads();
    const state = noDetail<string, string>();
    const detail = new Detail(state, read);
    const first = detail.show('a');
    const second = detail.show('b');
    pending[1]!.resolve('B');
    await second;
    pending[0]!.reject(new Error('late'));
    await first;
    expect(state).toEqual({ key: 'b', value: 'B', loading: false, error: null });
  });

  it('shows no value after a failure to read it', async () => {
    const { pending, read } = reads();
    const state = noDetail<string, string>();
    const detail = new Detail(state, read);
    const first = detail.show('a');
    pending[0]!.resolve('A');
    await first;
    const again = detail.reload();
    const failure = new Error('gone');
    pending[1]!.reject(failure);
    await again;
    expect(state).toEqual({ key: 'a', value: null, loading: false, error: failure });
  });

  it('clears, dropping an answer on its way; a reload then reads nothing', async () => {
    const { asked, pending, read } = reads();
    const state = noDetail<string, string>();
    const detail = new Detail(state, read);
    const shown = detail.show('a');
    detail.clear();
    pending[0]!.resolve('A');
    await shown;
    await detail.reload();
    expect(asked).toEqual(['a']);
    expect(state).toEqual(noDetail());
  });

  it('never reads its state, so a view effect that calls it does not depend on it', async () => {
    const { pending, read } = reads();
    const touched: string[] = [];
    const state = new Proxy(noDetail<string, string>(), {
      get(target, name) {
        touched.push(String(name));
        return Reflect.get(target, name);
      },
    });
    const detail = new Detail(state, read);
    const shown = detail.show('a');
    void detail.reload();
    pending[0]!.resolve('A');
    pending[1]!.resolve('A');
    await shown;
    expect(touched).toEqual([]);
  });
});
