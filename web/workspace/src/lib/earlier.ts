// SPDX-License-Identifier: MIT
/**
 * A history read back a page at a time: the entries before those shown, then the ones
 * before those, each page naming the cursor of the next earlier one (a comment thread's
 * entries, a finding's transitions). Built on `PagedList`, so a reset drops a page still
 * on its way. Each page is one item of the list, newest page first; `entries` puts them
 * back in order, oldest first. The view makes the state with `$state`.
 */
import { PagedList, type ListState, type Page } from './pagedList';

export class Earlier<T> {
  readonly list: PagedList<T[], number>;

  constructor(
    readonly state: ListState<T[], number>,
    /** The page before `before`; null asks for the first page back. */
    read: (before: number | null) => Promise<Page<T[], number>>,
  ) {
    this.list = new PagedList(state, read);
  }

  /** Whether any page has been read since the last reset. */
  get started(): boolean {
    return this.state.items.length > 0;
  }

  /** Whether there is an earlier page to read; `first` says so before any is read. */
  available(first = true): boolean {
    return this.started ? this.state.next !== null : first;
  }

  /** The next earlier page: the first page back, or the one before the last read. */
  read(): Promise<void> {
    return this.started ? this.list.more() : this.list.reload();
  }

  /** The entries read so far, oldest first. */
  entries(): T[] {
    return [...this.state.items].reverse().flat();
  }

  /** Nothing read; a page on its way is dropped (the history shown has changed). */
  reset(): void {
    this.list.clear();
  }
}
