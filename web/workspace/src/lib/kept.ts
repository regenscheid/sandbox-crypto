// SPDX-License-Identifier: MIT
/** A map that keeps its most recently used entries, within a count and a total weight. */
export class Kept<V> {
  readonly #map = new Map<string, V>();
  #weight = 0;

  constructor(
    private readonly maxEntries: number,
    private readonly maxWeight = Number.POSITIVE_INFINITY,
    private readonly weigh: (key: string, value: V) => number = () => 0,
  ) {}

  get size(): number {
    return this.#map.size;
  }

  /** The sum of the entries' weights. */
  get weight(): number {
    return this.#weight;
  }

  has(key: string): boolean {
    return this.#map.has(key);
  }

  get(key: string): V | undefined {
    const value = this.#map.get(key);
    if (value !== undefined) {
      this.#map.delete(key);
      this.#map.set(key, value);
    }
    return value;
  }

  set(key: string, value: V): void {
    const old = this.#map.get(key);
    if (old !== undefined) {
      this.#map.delete(key);
      this.#weight -= this.weigh(key, old);
    }
    const weight = this.weigh(key, value);
    // An entry heavier than the whole limit is not kept, and leaves the others be.
    if (weight > this.maxWeight) return;
    this.#map.set(key, value);
    this.#weight += weight;
    while (this.#map.size > this.maxEntries || this.#weight > this.maxWeight) {
      const [oldestKey, oldestValue] = this.#map.entries().next().value!;
      this.#map.delete(oldestKey);
      this.#weight -= this.weigh(oldestKey, oldestValue);
    }
  }

  clear(): void {
    this.#map.clear();
    this.#weight = 0;
  }
}
