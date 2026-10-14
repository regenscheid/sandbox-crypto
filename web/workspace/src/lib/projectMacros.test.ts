// SPDX-License-Identifier: MIT
import { afterEach, expect, it, vi } from 'vitest';
import { render } from 'svelte/server';
import { api } from './api';
import { macroSet } from './macros';
import { renderMarkdown, textState } from './markdown';
import { ModelRepairs, repairKey } from './mathrepair';
import FindingCard from '../views/FindingCard.svelte';
import type { Finding } from './reviews';

afterEach(() => vi.unstubAllGlobals());

it('separates render and repair cache identity by project and immutable context', () => {
  const p = macroSet({ '\\F': 'x' }, 'p:1'),
    q = macroSet({ '\\F': 'x' }, 'q:1');
  const changed = macroSet({ '\\F': 'y' }, 'p:2');
  expect(p.key).not.toBe(q.key);
  expect(p.key).not.toBe(changed.key);
  expect(repairKey('\\F', false, p.key)).not.toBe(repairKey('\\F', false, q.key));
  const kept = textState();
  const first = renderMarkdown('$\\F$', undefined, kept, p).html;
  const second = renderMarkdown('$\\F$', undefined, kept, changed).html;
  expect(first).toContain('>x<');
  expect(second).toContain('>y<');
});

it('writes only a scoped explicit settings payload and retains operation identity', async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ revision: 1 }) });
  vi.stubGlobal('fetch', fetch);
  const payload = { expected: 0, macros: { '\\F': 'x' }, operation_id: 'stable' };
  await api.changeMacros('p/one', payload);
  expect(fetch.mock.calls[0]?.[0]).toBe('/api/v0/projects/p%2Fone/macros');
  expect(JSON.parse(fetch.mock.calls[0]?.[1]?.body)).toEqual(payload);
});

it('carries stable project and context identity into repair requests', async () => {
  const transport = vi.fn().mockResolvedValue({ enabled: false, reason: 'fixture' });
  const repairs = new ModelRepairs(transport);
  renderMarkdown('$\\frac{1$', repairs, textState(), macroSet({}, 'p:42:abcdef'));
  await repairs.settled();
  expect(transport).toHaveBeenCalledOnce();
  expect(transport.mock.calls[0]?.[0]).toMatchObject({ project: 'p', macro_context: '42:abcdef' });
});

it('shows ambiguous anchored prose as diagnostic without hiding the warning', () => {
  const finding: Finding = {
    finding_id: 'finding-exact',
    review_id: 'review-exact',
    origin: 'review',
    severity: 'warn',
    category: 'contradicting_value',
    statement: '$\\F$',
    remedy: null,
    status: 'inconclusive',
    revision: 1,
    reflags: 0,
    anchors: [],
    macro_context: {
      project: 'p',
      revision: 1,
      sha256: 'a'.repeat(64),
      macros: { '\\F': 'project' },
      key: 'ambiguous',
      problems: ['Conflicting anchored document macro contexts; checks are held'],
    },
  };
  const html = render(FindingCard, {
    props: {
      projectId: 'p',
      sessionId: 's',
      finding,
      changed: async () => {},
      read: async () => {},
    },
  }).body;
  expect(html).toContain('Conflicting anchored document macro contexts');
  expect(html).toContain('Display uses a diagnostic context');
});
