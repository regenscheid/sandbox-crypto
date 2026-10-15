// SPDX-License-Identifier: MIT
// A copied-number finding's two numbers, lined up digit by digit on the finding card, with
// the places they differ marked (CR-39).
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import CopiedNumbers from '../views/CopiedNumbers.svelte';
import FindingCard from '../views/FindingCard.svelte';
import { copiedValues, digitRows, lineUp } from './copiedNumbers';
import type { Finding } from './reviews';

/** The statement as the platform's check writes it (consistency/numbers.py, `_statement`). */
const statement = (stated: string, held: string) =>
  "The platform's check of copied numbers (CR-39): R-1's statement states a " +
  `${stated.length}-digit number that no pinned artifact holds; artifact a1, at \`n\`, holds ` +
  `a ${held.length}-digit number 2 edits from it, the first difference at digit 9. ` +
  `Stated: ${stated}. In the artifact: ${held}. It looks copied by hand.`;

const pairs = (columns: { stated: string; held: string }[] | null) =>
  columns?.map((c) => `${c.stated || '-'}${c.held || '-'}`).join(' ');

/** The text of each element of `tag` in server-rendered HTML. */
const texts = (html: string, tag: string) =>
  [...html.matchAll(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, 'g'))].map((m) =>
    m[1]!.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]+>/g, ''),
  );

describe("a copied-number finding's two numbers", () => {
  it('are read from the statement of a contradicting value only', () => {
    const finding = {
      category: 'contradicting_value',
      statement: statement('1234567890', '1234567809'),
    };
    expect(copiedValues(finding)).toEqual({ stated: '1234567890', held: '1234567809' });
    expect(copiedValues({ ...finding, category: 'unsupported_claim' })).toBeNull();
    expect(copiedValues({ ...finding, statement: 'Stated: 12. In the artifact: x.' })).toBeNull();
  });

  it('line up as few edits apart as they are', () => {
    expect(pairs(lineUp('1234567890', '1234567809'))).toBe('11 22 33 44 55 66 77 88 90 09');
    expect(pairs(lineUp('1234567890', '12345067890'))).toBe('11 22 33 44 55 -0 66 77 88 99 00');
    expect(pairs(lineUp('12345678901', '1234578901'))).toBe('11 22 33 44 55 6- 77 88 99 00 11');
    expect(lineUp('1'.repeat(2500), '2'.repeat(2500))).toBeNull();
    expect(digitRows(lineUp('1'.repeat(85), '1'.repeat(85))!).map((row) => row.from)).toEqual([
      1, 41, 81,
    ]);
  });

  it('show one above the other, with the places that differ marked', () => {
    const html = render(CopiedNumbers, {
      props: { stated: '1234567890', held: '12345067809' },
    }).body;
    expect(html).toContain('3 places differ');
    expect(texts(html, 'mark')).toEqual(['–', '9', '0', '0', '0', '9']);
    expect(texts(html, 'pre')).toEqual([
      'Stated           1 12345–67890\nIn the artifact  1 12345067809',
    ]);
    const long = render(CopiedNumbers, {
      props: { stated: '1'.repeat(2500), held: '2'.repeat(2500) },
    }).body;
    expect(long).toContain('too long to line up here');
    expect(texts(long, 'mark')).toEqual([]);
  });

  it('show on the card of a contradicting value, and on no other', () => {
    const card = (category: string) =>
      render(FindingCard, {
        props: {
          projectId: 'p',
          sessionId: 's',
          changed: async () => {},
          finding: {
            finding_id: 'finding-1',
            review_id: null,
            severity: 'fail',
            category,
            statement: statement('1234567890', '1234567809'),
            remedy: null,
            status: 'open',
            revision: 1,
            reflags: 0,
            anchors: [],
          } as unknown as Finding,
        },
      }).body;
    expect(card('contradicting_value')).toContain('2 places differ');
    expect(card('unsupported_claim')).not.toContain('places differ');
  });
});
