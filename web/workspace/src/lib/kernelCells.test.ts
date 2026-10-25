// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { TreeRun } from './api-types';
import type { StepCall } from './conversation';
import { currentCell, kernelCells, kernelNotObserved, type PendingCall } from './kernelCells';
import type { KernelInfo } from './kernels';

const run = (name: string, fields: Partial<TreeRun> = {}): TreeRun => ({
  name,
  run_id: `id-${name}`,
  role: 'delegated',
  parent: 'main',
  delegation: null,
  started_at: '2026-10-25T10:00:00Z',
  state: 'running',
  stopped: null,
  kept: true,
  alias: null,
  limits: null,
  ...fields,
});
const main = run('main', { role: 'main', parent: null });
const child = run('child');
const other = run('other');

const kernel = (
  id: string,
  language: KernelInfo['language'],
  owner: TreeRun | null,
): KernelInfo => ({
  kernel_id: id,
  language,
  generation: 1,
  run: owner?.run_id ?? null,
  start_revision: 1,
  current_revision: 1,
  revision_status: 'current',
});
const pending = (
  owner: TreeRun,
  name: string,
  at: string,
  id = `${owner.name}-${name}-${at}`,
): PendingCall => ({
  run: owner,
  at,
  call: {
    id,
    name,
    arguments: '{"code":"x = 1"}',
    done: false,
    outcome: '',
    result: '',
  } as StepCall,
});

describe('a kernel’s current cell', () => {
  const session = kernel('k-session', 'python', null);
  const mine = kernel('k-child', 'python', child);

  it('joins the session’s kernel to the main run’s python call', () => {
    const call = pending(main, 'python', '2026-10-25T10:01:00Z');
    expect(currentCell(session, [call])).toBe(call);
  });

  it('joins a delegated run’s kernel to that run’s call, and no one else’s', () => {
    const own = pending(child, 'python', '2026-10-25T10:02:00Z');
    const others = [
      pending(main, 'python', '2026-10-25T10:00:00Z'),
      pending(other, 'python', '2026-10-25T10:00:30Z'),
    ];
    expect(currentCell(mine, [...others, own])).toBe(own);
    // The delegated run’s call does not run in the session’s kernel.
    expect(currentCell(session, [own])).toBeNull();
  });

  it('does not join a call of another language', () => {
    const calls = [
      pending(main, 'sage', '2026-10-25T10:01:00Z'),
      pending(main, 'shell', '2026-10-25T10:01:00Z'),
    ];
    expect(currentCell(session, calls)).toBeNull();
    expect(currentCell(kernel('k-sage', 'sage', null), calls)).toBe(calls[0]);
  });

  it('is idle without a pending call', () => {
    expect(currentCell(session, [])).toBeNull();
    expect([...kernelCells([session, mine], [])]).toEqual([
      ['k-session', null],
      ['k-child', null],
    ]);
  });

  it('is the oldest of two pending calls', () => {
    const late = pending(main, 'python', '2026-10-25T10:05:00Z');
    const early = pending(main, 'python', '2026-10-25T10:03:00Z');
    expect(currentCell(session, [late, early])).toBe(early);
    expect(currentCell(session, [early, late])).toBe(early);
  });
});

describe('a call whose kernel was not observed', () => {
  const python = pending(main, 'python', '2026-10-25T10:01:00Z');
  it('is flagged without an observation, or with one lacking its kernel', () => {
    expect(kernelNotObserved(python, null)).toBe(true);
    expect(kernelNotObserved(python, [])).toBe(true);
    expect(kernelNotObserved(python, [kernel('k', 'sage', null)])).toBe(true);
    expect(kernelNotObserved(python, [kernel('k', 'python', child)])).toBe(true);
  });
  it('is not when its kernel is there, nor for a call that needs none', () => {
    expect(kernelNotObserved(python, [kernel('k', 'python', null)])).toBe(false);
    expect(kernelNotObserved(pending(main, 'shell', python.at), null)).toBe(false);
  });
});
