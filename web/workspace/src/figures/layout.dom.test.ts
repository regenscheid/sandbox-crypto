// SPDX-License-Identifier: MIT
// A framed page's size (FG-14, FG-17): a frame of another site in a page the browser is
// not showing may have no width at first; the page waits for one, draws anyway after a
// while, and sends its size again, at a bounded rate, whenever it changes.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { laidOut, watchSize } from './layout';

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** An observer the test drives: `resize()` calls every live observer. */
function observers() {
  const live = new Set<{ seen: () => void }>();
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(readonly seen: () => void) {}
      observe() {
        live.add(this);
      }
      disconnect() {
        live.delete(this);
      }
    },
  );
  return {
    resize: () => [...live].forEach((observer) => observer.seen()),
    get count() {
      return live.size;
    },
  };
}

/** An element whose width the test sets. */
function sized(width: { value: number }, height = { value: 0 }): HTMLElement {
  const element = document.createElement('div');
  vi.spyOn(element, 'getBoundingClientRect').mockImplementation(
    () => ({ width: width.value, height: height.value }) as DOMRect,
  );
  return element;
}

describe('a frame with no width yet', () => {
  it('draws at once when it has a width', async () => {
    observers();
    expect(await laidOut(sized({ value: 545 }))).toBe(545);
  });

  it('waits for its first width, whose first size is 0', async () => {
    const observed = observers();
    const width = { value: 0 };
    let found: number | null = null;
    void laidOut(sized(width), 60_000).then((value) => (found = value));
    observed.resize(); // still no width
    await Promise.resolve();
    expect(found).toBeNull();
    width.value = 545;
    observed.resize();
    await Promise.resolve();
    expect(found).toBe(545);
    expect(observed.count).toBe(0);
  });

  it('draws at its own size when no width comes in time', async () => {
    vi.useFakeTimers();
    const observed = observers();
    let found: number | null = null;
    void laidOut(sized({ value: 0 }), 1500).then((value) => (found = value));
    await vi.advanceTimersByTimeAsync(1499);
    expect(found).toBeNull();
    await vi.advanceTimersByTimeAsync(1);
    expect(found).toBe(0);
    expect(observed.count).toBe(0);
  });
});

describe('a drawn frame resized', () => {
  it('sends each new size, at most one every 200 ms, the latest last', async () => {
    vi.useFakeTimers();
    const observed = observers();
    const width = { value: 0 };
    const height = { value: 0 };
    const element = sized(width, height);
    const sent: number[][] = [];
    const stop = watchSize(
      [element],
      () => element.getBoundingClientRect(),
      (w, h) => sent.push([w, h]),
    );
    observed.resize(); // unchanged: nothing sent
    expect(sent).toEqual([]);
    [width.value, height.value] = [545, 392];
    observed.resize();
    expect(sent).toEqual([[545, 392]]);
    for (const value of [300, 310, 320]) {
      width.value = value;
      observed.resize();
    }
    expect(sent).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(200);
    expect(sent).toEqual([
      [545, 392],
      [320, 392],
    ]);
    stop();
    width.value = 100;
    observed.resize();
    await vi.advanceTimersByTimeAsync(400);
    expect(sent).toHaveLength(2);
    expect(observed.count).toBe(0);
  });
});
