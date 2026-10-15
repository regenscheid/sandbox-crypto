// SPDX-License-Identifier: MIT
import { describe, expect, test } from 'vitest';
import type { ArtifactEntry } from './api-types';
import {
  ANCHOR_MESSAGE,
  anchorText,
  MAX_QUOTE,
  receivedAnchor,
  sizeText,
  viewable,
  viewerOrigin,
  visible,
  type ViewerFrame,
} from './pdfAnchors';

const SHA = 'ab'.repeat(32);
const frameWindow = { name: 'the frame' };
const FRAME: ViewerFrame = {
  origin: 'http://127.0.0.1:8201',
  window: frameWindow,
  sha256: SHA,
  path: 'sessions/a/paper.pdf',
};
const sent = {
  sha256: SHA,
  page: { index: 3, label: '1' },
  box: [71.96, 695.75, 254.93, 715.75],
  quote: 'Theorem 1.',
};
const event = (data: unknown, changes: Partial<{ origin: string; source: unknown }> = {}) => ({
  origin: FRAME.origin,
  source: frameWindow,
  data,
  ...changes,
});

describe('a message from the viewer frame', () => {
  test('is an anchor into the PDF the frame shows, with its path', () => {
    const anchor = receivedAnchor(event({ type: ANCHOR_MESSAGE, anchor: sent }), FRAME);
    expect(anchor).toEqual({ ...sent, path: 'sessions/a/paper.pdf' });
    expect(JSON.parse(anchorText(anchor!))).toEqual(anchor);
  });

  test.each([
    ['another origin', event({ type: ANCHOR_MESSAGE, anchor: sent }, { origin: 'http://evil' })],
    [
      'the workspace itself',
      event({ type: ANCHOR_MESSAGE, anchor: sent }, { origin: 'http://127.0.0.1:8200' }),
    ],
    ['another window', event({ type: ANCHOR_MESSAGE, anchor: sent }, { source: {} })],
    ['no window', event({ type: ANCHOR_MESSAGE, anchor: sent }, { source: null })],
  ])('is ignored from %s', (_, message) => {
    expect(receivedAnchor(message, FRAME)).toBeNull();
  });

  test('is ignored when the frame has no window', () => {
    const message = event({ type: ANCHOR_MESSAGE, anchor: sent }, { source: null });
    expect(receivedAnchor(message, { ...FRAME, window: null })).toBeNull();
  });

  test.each([
    ['another type', { type: 'other', anchor: sent }],
    ['an extra field', { type: ANCHOR_MESSAGE, anchor: sent, more: 1 }],
    ['no anchor', { type: ANCHOR_MESSAGE }],
    ['a string', JSON.stringify({ type: ANCHOR_MESSAGE, anchor: sent })],
    ['another PDF', { type: ANCHOR_MESSAGE, anchor: { ...sent, sha256: 'cd'.repeat(32) } }],
    [
      'an anchor with a path of its own',
      { type: ANCHOR_MESSAGE, anchor: { ...sent, path: '/etc' } },
    ],
    ['page 0', { type: ANCHOR_MESSAGE, anchor: { ...sent, page: { index: 0, label: '0' } } }],
    [
      'a fractional page',
      { type: ANCHOR_MESSAGE, anchor: { ...sent, page: { index: 1.5, label: '1' } } },
    ],
    [
      'a label that is not text',
      { type: ANCHOR_MESSAGE, anchor: { ...sent, page: { index: 1, label: 1 } } },
    ],
    [
      'a long label',
      { type: ANCHOR_MESSAGE, anchor: { ...sent, page: { index: 1, label: 'x'.repeat(201) } } },
    ],
    ['a short box', { type: ANCHOR_MESSAGE, anchor: { ...sent, box: [1, 2, 3] } }],
    ['a box with a non-number', { type: ANCHOR_MESSAGE, anchor: { ...sent, box: [1, 2, 3, NaN] } }],
    ['a sparse box', { type: ANCHOR_MESSAGE, anchor: { ...sent, box: [1, 2, 3, ,] } }],
    ['a box of strings', { type: ANCHOR_MESSAGE, anchor: { ...sent, box: ['1', 2, 3, 4] } }],
    [
      'a quote too long',
      { type: ANCHOR_MESSAGE, anchor: { ...sent, quote: 'x'.repeat(MAX_QUOTE + 1) } },
    ],
    [
      'a quote that is not text',
      { type: ANCHOR_MESSAGE, anchor: { ...sent, quote: { html: '<b>' } } },
    ],
  ])('is ignored with %s', (_, data) => {
    expect(receivedAnchor(event(data), FRAME)).toBeNull();
  });
});

test('hidden characters in a quote or a label are shown, and whitespace collapsed', () => {
  const tricky = {
    ...sent,
    page: { index: 3, label: 'iv\u202E' },
    quote: 'pay \u202Eolleh\u202C to\n\tme\u0007',
  };
  const anchor = receivedAnchor(event({ type: ANCHOR_MESSAGE, anchor: tricky }), FRAME);
  expect(anchor?.page.label).toBe('iv⟨U+202E⟩');
  expect(anchor?.quote).toBe('pay ⟨U+202E⟩olleh⟨U+202C⟩ to me⟨U+0007⟩');
  expect(visible('شكرا\u2067x\u2069')).toBe('شكرا⟨U+2067⟩x⟨U+2069⟩');
  expect(visible('ok ✓ é')).toBe('ok ✓ é');
});

test("the viewer's address must be http(s)", () => {
  expect(viewerOrigin('http://127.0.0.1:8201/viewer/#c=x&w=y')).toBe('http://127.0.0.1:8201');
  expect(viewerOrigin('javascript:alert(1)')).toBeNull();
  expect(viewerOrigin('data:text/html,hi')).toBeNull();
  expect(viewerOrigin('not a url')).toBeNull();
});

test('only PDFs open in the viewer, and only when the server has it on', () => {
  const entry = {
    artifact_id: 'art-1',
    revision: 1,
    path: 'paper.pdf',
    sha256: SHA,
    size: 10,
    media_type: 'application/pdf',
    created_at: '2026-10-14T00:00:00Z',
    result: true,
    deleted: false,
    content_retained: true,
    content_note: null,
    kind: 'agent_publication',
    session_id: 's1',
    run_id: 'run-1',
    role: 'main',
    tool_call: 'call1',
  } satisfies ArtifactEntry;
  expect(viewable(entry, true)).toBe(true);
  expect(viewable(entry, false)).toBe(false);
  expect(viewable({ ...entry, media_type: 'text/plain' }, true)).toBe(false);
});

test('sizes read in bytes, KiB and MiB', () => {
  expect(sizeText(512)).toBe('512 B');
  expect(sizeText(1536)).toBe('1.5 KiB');
  expect(sizeText(3 * 1024 * 1024)).toBe('3.0 MiB');
});
