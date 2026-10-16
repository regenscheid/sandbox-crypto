// SPDX-License-Identifier: MIT
// The figure frame's protocol (FG-14): it takes one request, in an exact shape, from the
// workspace's origin and its parent window only; the workspace takes a status, in an
// exact shape, back. Anything else is ignored.
import { describe, expect, test } from 'vitest';
import {
  accepted,
  figureRequest,
  figureStatus,
  frameWorkspace,
  MAX_CAPABILITIES,
  statusMessage,
} from './protocol';

const CAP = `${'a'.repeat(40)}.${'b'.repeat(43)}`;
const WORKSPACE = 'http://127.0.0.1:8200';
const request = { type: 'figure', kind: 'svg', source: '', capabilities: [CAP] };

describe('the request the frame takes', () => {
  test('is the exact shape, and nothing else', () => {
    expect(figureRequest(request)).toEqual(request);
    for (const bad of [
      null,
      'figure',
      [request],
      { ...request, extra: 1 },
      { ...request, type: 'other' },
      { ...request, kind: 'html' }, // an HTML file is never drawn by this page
      { ...request, kind: 'vega-lite' }, // not yet (F5)
      { ...request, source: 'x' }, // an SVG file has no text of its own
      { ...request, capabilities: [] },
      { ...request, capabilities: [CAP, CAP] },
      { ...request, capabilities: ['javascript:alert(1)'] },
      { ...request, capabilities: [`../${CAP}`] },
      { ...request, capabilities: [`${CAP}?x=1`] },
      { ...request, capabilities: Array(MAX_CAPABILITIES + 1).fill(CAP) },
      { type: 'figure', kind: 'svg', source: '' },
    ])
      expect(figureRequest(bad)).toBeNull();
  });

  test('comes from the workspace origin and the parent window only', () => {
    const parent = {};
    expect(
      accepted({ origin: WORKSPACE, source: parent, data: request }, WORKSPACE, parent),
    ).toEqual(request);
    // Another origin, even the preview origin itself, or another window, is ignored.
    expect(
      accepted(
        { origin: 'http://127.0.0.1:8201', source: parent, data: request },
        WORKSPACE,
        parent,
      ),
    ).toBeNull();
    expect(
      accepted({ origin: WORKSPACE, source: {}, data: request }, WORKSPACE, parent),
    ).toBeNull();
    expect(
      accepted({ origin: 'null', source: parent, data: request }, WORKSPACE, parent),
    ).toBeNull();
  });

  test('names the workspace in the fragment, an http(s) origin exactly', () => {
    expect(frameWorkspace(`#w=${encodeURIComponent(WORKSPACE)}`)).toBe(WORKSPACE);
    expect(frameWorkspace(`#w=${encodeURIComponent(`${WORKSPACE}/x`)}`)).toBeNull();
    expect(frameWorkspace('#w=javascript%3Aalert(1)')).toBeNull();
    expect(frameWorkspace('#w=*')).toBeNull();
    expect(frameWorkspace('')).toBeNull();
  });
});

describe('the status the workspace takes', () => {
  test('is the frame size and a status, bounded, in the exact shape', () => {
    const rendered = statusMessage('rendered', 640.4, 480.6);
    expect(rendered).toEqual({
      type: 'figure-status',
      status: 'rendered',
      width: 640,
      height: 481,
      reason: '',
    });
    expect(figureStatus(rendered)).toEqual(rendered);
    expect(statusMessage('error', NaN, -3, 'x'.repeat(400)).reason).toHaveLength(300);
    for (const bad of [
      { ...rendered, extra: true },
      { ...rendered, status: 'drawn' },
      { ...rendered, height: 1e9 },
      { ...rendered, height: '480' },
      { ...rendered, width: 1.5 },
      { ...rendered, reason: '<script>'.repeat(100) },
      { type: 'figure-status', status: 'rendered' },
    ])
      expect(figureStatus(bad)).toBeNull();
  });
});
