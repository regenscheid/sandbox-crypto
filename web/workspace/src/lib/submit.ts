// SPDX-License-Identifier: MIT
/**
 * How a view runs a write (a save through `guardedWrite`, mutation.ts, or another
 * change): one at a time, busy while it runs, its failure kept for the view to show. The
 * view makes the state with `$state`; a test passes a plain object.
 */

export interface Submitting {
  busy: boolean;
  /** The last write's failure, cleared when the next one starts. */
  error: unknown;
}

export function idle(): Submitting {
  return { busy: false, error: null };
}

/**
 * Run `write` unless another is running; whether it finished. A failure is kept in
 * `state.error`, and `failed`, if given, runs after it (to read the current revision,
 * say): its own failure is ignored, since the write's is the one to show.
 */
export async function submit(
  state: Submitting,
  write: () => Promise<void>,
  failed?: () => Promise<void>,
): Promise<boolean> {
  if (state.busy) return false;
  state.busy = true;
  state.error = null;
  try {
    await write();
    return true;
  } catch (caught) {
    state.error = caught;
    try {
      await failed?.();
    } catch {
      /* The write's failure is the one shown. */
    }
    return false;
  } finally {
    state.busy = false;
  }
}
