// SPDX-License-Identifier: MIT
/**
 * A pinned evidence text read slice by slice (docs/CONSISTENCY-REVIEW.md), for whichever
 * part of the reviews panel asked for it: a review's detail, or a finding's card. A text
 * asked for replaces the one shown only when its first slice is shown, so after a failed
 * read the next slice still continues the text on screen, and two texts are never spliced.
 */
import { PagedList, type ListState } from './pagedList';

interface Key {
  review: string;
  evidence: string;
}

export class EvidenceText {
  readonly list: PagedList<string, number>;
  #reading: Key = { review: '', evidence: '' };
  #asked: Key = { review: '', evidence: '' };

  constructor(
    readonly state: ListState<string, number>,
    slice: (
      review: string,
      evidence: string,
      start: number,
    ) => Promise<{ text: string; next_start: number | null }>,
  ) {
    this.list = new PagedList(state, async (start) => {
      const key = start === null ? this.#asked : this.#reading;
      const found = await slice(key.review, key.evidence, start ?? 0);
      return {
        items: [found.text],
        next: found.next_start,
        shown: () => void (this.#reading = key),
      };
    });
  }

  /** Read an evidence text from its start, in place of the one being read. */
  read(review: string, evidence: string): Promise<void> {
    this.#asked = { review, evidence };
    return this.list.reload();
  }

  /** The text read so far. */
  text(): string {
    return this.state.items.join('');
  }
}
