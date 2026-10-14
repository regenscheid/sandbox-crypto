// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { idle, submit } from './submit';

describe('running a write', () => {
  it('is busy while the write runs and says it finished', async () => {
    const state = idle();
    let release = () => {};
    const write = submit(state, () => new Promise<void>((resolve) => (release = resolve)));
    expect(state.busy).toBe(true);
    release();
    expect(await write).toBe(true);
    expect(state).toEqual({ busy: false, error: null });
  });

  it('runs one write at a time', async () => {
    const state = idle();
    let release = () => {};
    const first = submit(state, () => new Promise<void>((resolve) => (release = resolve)));
    let ran = false;
    expect(
      await submit(state, async () => {
        ran = true;
      }),
    ).toBe(false);
    expect(ran).toBe(false);
    release();
    await first;
  });

  it('keeps the failure, then reads the current state, whose own failure is not shown', async () => {
    const state = idle();
    const failure = new Error('conflict');
    const order: string[] = [];
    const finished = await submit(
      state,
      async () => {
        order.push('write');
        throw failure;
      },
      async () => {
        order.push('refresh');
        throw new Error('offline');
      },
    );
    expect(finished).toBe(false);
    expect(order).toEqual(['write', 'refresh']);
    expect(state).toEqual({ busy: false, error: failure });
  });

  it('clears the last failure when the next write starts', async () => {
    const state = { busy: false, error: new Error('earlier') as unknown };
    const write = submit(state, async () => {
      expect(state.error).toBeNull();
    });
    expect(await write).toBe(true);
  });
});
