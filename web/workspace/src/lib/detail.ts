// SPDX-License-Identifier: MIT
/**
 * One thing read by its key and shown in detail, such as a document's history or a
 * patch's preview. Only the latest choice is shown: an answer for a key chosen earlier is
 * dropped when it arrives late, with its failure. The view makes the state with `$state`
 * and the detail changes it in place; a test passes a plain object. The detail only
 * writes the state, never reads it, so a view's effect may call it without depending on it.
 */

export interface DetailState<K, T> {
  key: K | null;
  /** The key's value; null until it arrives, and after a failure to read it. */
  value: T | null;
  loading: boolean;
  error: unknown;
}

export function noDetail<K, T>(): DetailState<K, T> {
  return { key: null, value: null, loading: false, error: null };
}

export class Detail<K, T> {
  #generation = 0;
  #key: K | null = null;

  constructor(
    readonly state: DetailState<K, T>,
    private readonly read: (key: K) => Promise<T>,
  ) {}

  /**
   * Read `key` and show it. Another key's value is cleared at once, so it is never shown
   * under this one; the same key's value stays until the new one replaces it.
   */
  async show(key: K): Promise<void> {
    const generation = ++this.#generation;
    const same = this.#key === key;
    this.#key = key;
    Object.assign(this.state, {
      key,
      loading: true,
      error: null,
      ...(same ? {} : { value: null }),
    });
    try {
      const value = await this.read(key);
      if (generation === this.#generation) this.state.value = value;
    } catch (caught) {
      if (generation === this.#generation)
        Object.assign(this.state, { value: null, error: caught });
    } finally {
      if (generation === this.#generation) this.state.loading = false;
    }
  }

  /** The shown key read again; nothing when none is shown. */
  reload(): Promise<void> {
    return this.#key === null ? Promise.resolve() : this.show(this.#key);
  }

  /** Nothing shown; answers still on their way are dropped. */
  clear(): void {
    this.#generation++;
    this.#key = null;
    Object.assign(this.state, noDetail<K, T>());
  }
}
