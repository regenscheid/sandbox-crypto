// SPDX-License-Identifier: MIT
/**
 * Each observed kernel's current cell (B10, item 5), joined from two reads the Compute tab
 * already makes: the worker's observation of the session's kernels and each running agent's
 * calls with no result yet. No executor reports a kernel's cell; it is the `python` or
 * `sage` call (the kernel's language) that the kernel's owner has made and has no result
 * for. The session's kernels (`run` null) belong to the main run; a delegated run's own
 * kernel names that run by its ID.
 */
import type { TreeRun } from './api-types';
import { rowLabel, type StepCall } from './conversation';
import type { KernelInfo } from './kernels';

/** A tool call without a result yet, with when it was made and the run that made it. */
export interface PendingCall {
  call: StepCall;
  at: string;
  run: TreeRun;
}

/** A kernel's language is the name of the tool that runs a cell in it. */
export const isCell = (call: Pick<StepCall, 'name'>): boolean =>
  call.name === 'python' || call.name === 'sage';

/** Whether a run's call of this language runs in this kernel. */
export function ownsKernel(kernel: Pick<KernelInfo, 'run' | 'language'>, item: PendingCall) {
  if (item.call.name !== kernel.language) return false;
  return kernel.run === null ? item.run.role === 'main' : kernel.run === item.run.run_id;
}

/** A kernel's current cell: the owner's oldest call of its language without a result, or null (idle). */
export function currentCell(
  kernel: Pick<KernelInfo, 'run' | 'language'>,
  pending: readonly PendingCall[],
): PendingCall | null {
  let oldest: PendingCall | null = null;
  for (const item of pending) {
    if (!ownsKernel(kernel, item)) continue;
    if (oldest === null || Date.parse(item.at) < Date.parse(oldest.at)) oldest = item;
  }
  return oldest;
}

/** The kernels' current cells by kernel ID; null where a kernel is idle. */
export function kernelCells(
  kernels: readonly KernelInfo[],
  pending: readonly PendingCall[],
): Map<string, PendingCall | null> {
  return new Map(kernels.map((kernel) => [kernel.kernel_id, currentCell(kernel, pending)]));
}

/**
 * Whether a cell call has no kernel in the observation to run in: no observation yet, a
 * stale, busy or unavailable one that lacks the kernel, or a kernel not started. Calls of
 * other tools never need one.
 */
export function kernelNotObserved(
  item: PendingCall,
  kernels: readonly KernelInfo[] | null,
): boolean {
  return isCell(item.call) && !(kernels ?? []).some((kernel) => ownsKernel(kernel, item));
}
