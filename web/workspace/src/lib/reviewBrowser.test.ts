// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { emptyReviewPanel, panelBusy, panelError, ReviewBrowser } from './reviewBrowser';
import type { reviews, Review } from './reviews';

type Api = typeof reviews;

const review = (id: string, summary = id) => ({ review_id: id, summary }) as unknown as Review;

/** A fake reviews API: each call is recorded and waits for the test to answer it. */
function fake() {
  const calls: {
    name: string;
    args: unknown[];
    resolve: (v: unknown) => void;
    reject: (e: unknown) => void;
  }[] = [];
  const call =
    (name: string) =>
    (...args: unknown[]) =>
      new Promise((resolve, reject) => calls.push({ name, args, resolve, reject }));
  const api = Object.fromEntries(
    ['list', 'findings', 'math', 'manifest', 'records', 'evidence', 'mathEvidence', 'request'].map(
      (name) => [name, call(name)],
    ),
  ) as unknown as Api;
  const answer = (name: string, value: unknown, index = 0) => {
    const found = calls.filter((item) => item.name === name)[index];
    if (!found) throw new Error(`no call to ${name}`);
    found.resolve(value);
  };
  return { api, calls, answer };
}

function browser() {
  const { api, calls, answer } = fake();
  const panel = emptyReviewPanel();
  const where = { project: 'p1', session: 's1' };
  return { panel, calls, answer, where, browse: new ReviewBrowser(panel, () => where, api) };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('the reviews panel', () => {
  it('reads reviews, findings and math checks with what their first pages bring', async () => {
    const { panel, answer, calls, browse } = browser();
    const done = browse.refresh();
    expect(panelBusy(panel)).toBe(true);
    expect(calls.map((call) => [call.name, call.args])).toEqual([
      ['list', ['p1', 's1', null]],
      ['findings', ['p1', 's1', null]],
      ['math', ['p1', 's1', null]],
    ]);
    answer('list', { reviews: [review('r1')], next_after: 'r1', unreviewed: [{ window: 1 }] });
    answer('findings', { findings: [], next_after: null });
    answer('math', { checks: [], next_after: null, queue: [{ source: 'd' }], queue_more: 1 });
    await done;
    expect(panelBusy(panel)).toBe(false);
    expect(panel.reviews.items).toEqual([review('r1')]);
    expect(panel.unreviewed).toEqual([{ window: 1 }]);
    expect(panel.queue).toEqual([{ source: 'd' }]);
    expect(panel.queueMore).toBe(true);
  });

  it('keeps the chosen review current when the list is read again', async () => {
    const { panel, answer, browse } = browser();
    panel.selected = review('r1', 'old');
    const done = browse.refresh();
    answer('list', { reviews: [review('r1', 'new')], next_after: null, unreviewed: [] });
    answer('findings', { findings: [], next_after: null });
    answer('math', { checks: [], next_after: null, queue: [] });
    await done;
    expect(panel.selected?.summary).toBe('new');
  });

  it("reads a chosen review's manifest, then its audit records from the start", async () => {
    const { panel, answer, calls, browse } = browser();
    const chosen = browse.select(review('r1'));
    expect(panel.selected).toEqual(review('r1'));
    expect(panel.records.next).toBeNull(); // not before the manifest
    answer('manifest', {
      review: review('r1', 'manifest'),
      evidence: [{ evidence: 'e1' }],
      next_after: 1,
    });
    await chosen;
    expect(panel.selected?.summary).toBe('manifest');
    expect(panel.evidence.items).toEqual([{ evidence: 'e1' }]);
    expect(panel.records).toMatchObject({ items: [], next: 0 });
    void browse.evidence.more();
    void browse.records.more();
    expect(calls.slice(-2).map((call) => [call.name, call.args])).toEqual([
      ['manifest', ['p1', 's1', 'r1', 1]],
      ['records', ['p1', 's1', 'r1', 0]],
    ]);
  });

  it('drops what was on its way for the review chosen before', async () => {
    const { panel, answer, browse } = browser();
    void browse.select(review('r1'));
    answer('manifest', { review: review('r1'), evidence: [{ evidence: 'e1' }], next_after: 1 });
    await settle();
    const late = browse.records.more();
    const second = browse.select(review('r2'));
    answer('records', { records: [{ sequence: 1 }], next_after: null });
    answer(
      'manifest',
      { review: review('r2'), evidence: [{ evidence: 'e2' }], next_after: null },
      1,
    );
    await Promise.all([late, second]);
    expect(panel.selected?.review_id).toBe('r2');
    expect(panel.evidence.items).toEqual([{ evidence: 'e2' }]);
    expect(panel.records).toMatchObject({ items: [], next: 0 });
  });

  it('reads an evidence text slice by slice, and starts again for another text', async () => {
    const { panel, answer, calls, browse } = browser();
    const first = browse.read('r1', 'e1');
    answer('evidence', { text: 'abc', next_start: 3 });
    await first;
    const next = browse.text.more();
    answer('evidence', { text: 'def', next_start: null }, 1);
    await next;
    expect(panel.text.items.join('')).toBe('abcdef');
    const other = browse.read('r1', 'e2');
    answer('evidence', { text: 'xyz', next_start: null }, 2);
    await other;
    expect(panel.text.items.join('')).toBe('xyz');
    expect(calls.filter((call) => call.name === 'evidence').map((call) => call.args)).toEqual([
      ['p1', 's1', 'r1', 'e1', 0],
      ['p1', 's1', 'r1', 'e1', 3],
      ['p1', 's1', 'r1', 'e2', 0],
    ]);
  });

  it("shows a math check's first page whole, then each next page's observations", async () => {
    const { panel, answer, browse } = browser();
    const first = browse.readMath('c1');
    const page = { check: { id: 'c1' }, observations: [{ n: 1 }], next_after: 5 };
    answer('mathEvidence', page);
    await first;
    const next = browse.math.more();
    answer('mathEvidence', { check: {}, observations: [{ n: 2 }], next_after: null }, 1);
    await next;
    expect(panel.math.items).toEqual([
      JSON.stringify(page, null, 2),
      JSON.stringify([{ n: 2 }], null, 2),
    ]);
  });

  it('shows the first failure, and the outcome of a review request', async () => {
    const { panel, calls, answer, browse } = browser();
    const done = browse.refresh();
    const failure = new Error('offline');
    calls[1]!.reject(failure);
    answer('list', { reviews: [], next_after: null, unreviewed: [] });
    answer('math', { checks: [], next_after: null, queue: [] });
    await done;
    expect(panelError(panel)).toBe(failure);
    const asked = browse.request();
    answer('request', { queued: true, detail: 'Queued for the next safe point.' });
    await asked;
    expect(panel.notice).toBe('Queued for the next safe point.');
  });
});
