// SPDX-License-Identifier: MIT
/**
 * A list read a page at a time: the first page, then the next on request, each page
 * naming the cursor of the one after it. The view makes the state with `$state` and the
 * list changes it in place; a test passes a plain object.
 *
 * A reload supersedes a page still on its way, so an answer that arrives late never mixes
 * an older list into a newer one, and never reports an error for a read nobody waits for.
 */

export interface Page<T, C> {
  items: T[];
  next: C | null;
  /**
   * What else the answer brings, applied only when the page is shown: a page that a
   * reload superseded changes nothing.
   */
  shown?: () => void;
}

export interface ListState<T, C> {
  items: T[];
  /** The next page's cursor; null when the list is complete. */
  next: C | null;
  loading: boolean;
  /** The last read's failure; the items read before it stay. */
  error: unknown;
}

export function emptyList<T, C>(): ListState<T, C> {
  return { items: [], next: null, loading: false, error: null };
}

export class PagedList<T, C> {
  #generation = 0;

  constructor(
    readonly state: ListState<T, C>,
    private readonly read: (cursor: C | null) => Promise<Page<T, C>>,
  ) {}

  /** The first page again, replacing the list. */
  reload(): Promise<void> {
    return this.#load(null);
  }

  /** The next page, appended; nothing when the list is complete or a page is on its way. */
  more(): Promise<void> {
    const { next, loading } = this.state;
    return next === null || loading ? Promise.resolve() : this.#load(next);
  }

  /** An empty list; pages still on their way are dropped. */
  clear(): void {
    this.#generation++;
    Object.assign(this.state, emptyList<T, C>());
  }

  async #load(cursor: C | null): Promise<void> {
    const generation = ++this.#generation;
    this.state.loading = true;
    this.state.error = null;
    try {
      const page = await this.read(cursor);
      if (generation !== this.#generation) return;
      this.state.items = cursor === null ? page.items : [...this.state.items, ...page.items];
      this.state.next = page.next;
      page.shown?.();
    } catch (caught) {
      if (generation === this.#generation) this.state.error = caught;
    } finally {
      if (generation === this.#generation) this.state.loading = false;
    }
  }
}
