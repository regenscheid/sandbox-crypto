// SPDX-License-Identifier: MIT
import { afterEach, expect, it } from 'vitest';
import type { DocumentRevision } from './api';
import { anchorFromSelection } from './documentSelection';

const found = {
  document: { document_id: 'doc1' },
  revision: { number: 4 },
  blocks: [
    { id: 'b1', source: 'Alpha $x$ and beta.', math: [{ id: 'm1', tex: 'x' }] },
    { id: 'b2', source: 'Repeat Repeat', math: [] },
  ],
} as unknown as DocumentRevision;

function select(start: Node, from: number, end: Node, to: number) {
  const range = document.createRange();
  range.setStart(start, from);
  range.setEnd(end, to);
  const selection = window.getSelection()!;
  selection.removeAllRanges();
  selection.addRange(range);
  return selection;
}

afterEach(() => {
  window.getSelection()?.removeAllRanges();
  document.body.replaceChildren();
});

it('anchors uniquely matched visible text to its source range', () => {
  const article = document.createElement('article');
  article.innerHTML = '<p data-block="b1">Alpha <span data-math="m1">x</span> and beta.</p>';
  document.body.append(article);
  const text = article.querySelector('p')!.firstChild!;
  expect(anchorFromSelection(select(text, 0, text, 5), article, found)).toEqual({
    documentId: 'doc1',
    revision: 4,
    block: 'b1',
    kind: 'range',
    math: '',
    quote: 'Alpha',
    note: '',
  });
});

it('uses the formula identity when selection is within the same formula', () => {
  const article = document.createElement('article');
  article.innerHTML = '<p data-block="b1"><span data-math="m1">x</span></p>';
  document.body.append(article);
  const text = article.querySelector('span')!.firstChild!;
  expect(anchorFromSelection(select(text, 0, text, 1), article, found)?.math).toBe('m1');
});

it('uses a block anchor when rendered text has no unique source match', () => {
  const article = document.createElement('article');
  article.innerHTML = '<p data-block="b2">Repeat Repeat</p>';
  document.body.append(article);
  const text = article.querySelector('p')!.firstChild!;
  const result = anchorFromSelection(select(text, 0, text, 6), article, found);
  expect(result?.kind).toBe('block');
  expect(result?.note).toContain('unique source range');
});

it('refuses selections that cross blocks or leave the article', () => {
  const article = document.createElement('article');
  article.innerHTML = '<p data-block="b1">Alpha</p><p data-block="b2">Repeat</p>';
  document.body.append(article);
  const [first, second] = article.querySelectorAll('p');
  expect(
    anchorFromSelection(select(first!.firstChild!, 0, second!.firstChild!, 3), article, found),
  ).toBeNull();
  const outside = document.createTextNode('outside');
  document.body.append(outside);
  expect(anchorFromSelection(select(first!.firstChild!, 0, outside, 3), article, found)).toBeNull();
});
