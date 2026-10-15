// SPDX-License-Identifier: MIT
import { describe, expect, test } from 'vitest';
import {
  anchorMessage,
  canvasRatio,
  capabilitySha,
  framedBy,
  MAX_CANVAS_PIXELS,
  MAX_QUOTE,
  pageByLabel,
  pageLabel,
  pdfBox,
  viewerParams,
} from './anchor';

const SHA = 'ab'.repeat(32);
const encode = (value: unknown) =>
  btoa(JSON.stringify(value)).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
const capability = (fields: unknown) => `${encode(fields)}.c2lnbmF0dXJl`;
const CAPABILITY = capability(['p1', 'art-1', 2, SHA, 1_791_990_600]);
const fragment = (c: string, w: string) => `#${new URLSearchParams({ c, w })}`;

describe('the URL fragment the workspace mints', () => {
  test('names the capability and the workspace origin', () => {
    expect(viewerParams(fragment(CAPABILITY, 'http://127.0.0.1:8200'))).toEqual({
      capability: CAPABILITY,
      workspace: 'http://127.0.0.1:8200',
      page: null,
    });
  });

  test('names the page to open at, a number from 1, or none', () => {
    const at = (p: string) =>
      viewerParams(`${fragment(CAPABILITY, 'http://127.0.0.1:8200')}&p=${p}`)?.page;
    expect(at('3')).toBe(3);
    expect(at('0')).toBeNull();
    expect(at('-2')).toBeNull();
    expect(at('2.5')).toBeNull();
    expect(at('1e3')).toBeNull();
    expect(at('1234567')).toBeNull();
  });

  test.each([
    ['no fragment', ''],
    ['a capability that is not one', fragment('a.b.c', 'http://127.0.0.1:8200')],
    ['a capability with a path', fragment('../api.x', 'http://127.0.0.1:8200')],
    ['no workspace', `#c=${CAPABILITY}`],
    ['a workspace with a path', fragment(CAPABILITY, 'http://127.0.0.1:8200/evil')],
    ['a workspace of another scheme', fragment(CAPABILITY, 'javascript:alert(1)')],
    ['a wildcard workspace', fragment(CAPABILITY, '*')],
  ])('is refused with %s', (_, hash) => {
    expect(viewerParams(hash)).toBeNull();
  });

  test("gives the capability's SHA-256, or nothing for a payload that is not one", () => {
    expect(capabilitySha(CAPABILITY)).toBe(SHA);
    expect(capabilitySha(capability(['p1', 'art-1', 2, 'not-a-sha', 0]))).toBeNull();
    expect(capabilitySha(capability({ sha256: SHA }))).toBeNull();
    expect(capabilitySha('!!!.x')).toBeNull();
  });
});

describe('pages by number and by printed label', () => {
  const labels = ['i', 'ii', '1', '2', ''];

  test('a page without labels, or without its own, is labelled by its number', () => {
    expect(pageLabel(null, 3)).toBe('3');
    expect(pageLabel(labels, 2)).toBe('ii');
    expect(pageLabel(labels, 5)).toBe('5');
  });

  test('a label names the first page that has it', () => {
    expect(pageByLabel(labels, ' 1 ', 5)).toBe(3);
    expect(pageByLabel(labels, 'ii', 5)).toBe(2);
    expect(pageByLabel(labels, '5', 5)).toBe(5);
    expect(pageByLabel(labels, 'iii', 5)).toBeNull();
    expect(pageByLabel(null, '7', 5)).toBeNull();
  });
});

describe('the anchor of a selection', () => {
  // A page shown at 2x, 200 points high: CSS pixels down from the top become points up.
  const toPdf = (x: number, y: number) => [x / 2, 200 - y / 2];

  test('is the box in PDF points that covers every rectangle', () => {
    const rects = [
      { left: 20, top: 40, right: 120, bottom: 60 },
      { left: 10, top: 60, right: 50.333, bottom: 80 },
    ];
    expect(pdfBox(rects, toPdf)).toEqual([5, 160, 60, 180]);
    expect(pdfBox([{ left: 1, top: 1, right: 2.0001, bottom: 3 }], toPdf)).toEqual([
      0.5, 198.5, 1, 199.5,
    ]);
  });

  test('ignores empty rectangles, and is nothing without one', () => {
    const empty = { left: 10, top: 10, right: 10, bottom: 30 };
    expect(pdfBox([empty], toPdf)).toBeNull();
    expect(pdfBox([], toPdf)).toBeNull();
    expect(pdfBox([empty, { left: 0, top: 0, right: 2, bottom: 2 }], toPdf)).toEqual([
      0, 199, 1, 200,
    ]);
  });

  test('carries the quote with its whitespace collapsed, and no longer than the limit', () => {
    const message = anchorMessage(SHA, 3, '1', [1, 2, 3, 4], '  a\n  quoted\tline ');
    expect(message).toEqual({
      type: 'sandbox-science/pdf-anchor',
      anchor: {
        sha256: SHA,
        page: { index: 3, label: '1' },
        box: [1, 2, 3, 4],
        quote: 'a quoted line',
      },
    });
    expect(
      anchorMessage(SHA, 1, '1', [0, 0, 1, 1], 'x'.repeat(MAX_QUOTE + 5)).anchor.quote,
    ).toHaveLength(MAX_QUOTE);
  });
});

describe('where the viewer runs', () => {
  const workspace = 'http://127.0.0.1:8200';

  test('inside the workspace the link names, or where the browser does not say', () => {
    expect(framedBy(workspace, [workspace])).toBe(true);
    expect(framedBy(workspace, [workspace, 'http://elsewhere'])).toBe(true);
    expect(framedBy(workspace, [])).toBe(true); // opened directly: anchors go nowhere
    expect(framedBy(workspace, undefined)).toBe(true); // a browser without the list
  });

  test('never inside another page', () => {
    expect(framedBy(workspace, ['http://evil.example'])).toBe(false);
    expect(framedBy(workspace, ['http://127.0.0.1:8201', workspace])).toBe(false);
  });
});

describe('the canvas of a page', () => {
  test("follows the screen's ratio while the canvas stays under the cap", () => {
    expect(canvasRatio(612, 792, 2)).toBe(2);
    expect(canvasRatio(612, 792, 1)).toBe(1);
  });

  test('is drawn at a lower resolution, not a smaller size, past the cap', () => {
    // A 200-inch-wide page at 300%: its CSS size is kept, its pixels are capped.
    const [width, height] = [43200, 2376];
    const ratio = canvasRatio(width, height, 2);
    expect(ratio).toBeLessThan(1);
    expect(Math.floor(width * ratio) * Math.floor(height * ratio)).toBeLessThanOrEqual(
      MAX_CANVAS_PIXELS,
    );
  });

  test('keeps the screen ratio for an empty page', () => {
    expect(canvasRatio(0, 792, 2)).toBe(2);
  });
});
