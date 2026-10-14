// SPDX-License-Identifier: MIT
import { afterEach, expect, it, vi } from 'vitest';
// Rendered HTML is compared with each whitespace run as one space, so that line breaks in
// the components' source do not matter.
import { render } from 'svelte/server';
import FindingCard from '../views/FindingCard.svelte';
import { reviews, type Finding } from './reviews';
import { findingDrafts } from './drafts';
import { Preview } from './preview';

afterEach(() => vi.unstubAllGlobals());
const finding: Finding = {
  finding_id: 'finding-mechanical',
  review_id: null,
  origin: 'math',
  source: {
    kind: 'math',
    check: 'math-exact',
    excluded: 'Current owner is USER; history remains unresolved.',
  },
  severity: 'warn',
  category: 'math',
  statement: 'Unknown command $\\unknown{x}$.',
  remedy: 'Use $x$.',
  status: 'open',
  revision: 4,
  reflags: 1,
  anchors: [{ check: 'math-exact', math: 'm1', locator: { document: 'doc-exact', revision: 2 } }],
};

it('renders mechanical authority, exact evidence and retained stale USER response draft', () => {
  findingDrafts.save('p:s:finding-mechanical', {
    expected: 3,
    explanation: 'Unsaved user explanation <script>',
    action: 'rebut',
    references: 'doc-old:1',
  });
  const html = render(FindingCard, {
    props: {
      projectId: 'p',
      sessionId: 's',
      finding,
      changed: async () => {},
    },
  }).body.replace(/\s+/g, ' ');
  expect(html).toContain('Mechanical math check');
  expect(html).toContain('No reviewer model was used.');
  expect(html).toContain('Read mechanical evidence');
  expect(html).toContain('math-exact');
  expect(html).toContain('history remains unresolved');
  expect(html).toContain('unavailable evidence does not resolve');
  expect(html).toContain('Based on finding revision 3');
  expect(html).toContain('Use current finding revision');
  expect(html).toContain('Unsaved user explanation &lt;script>');
  expect(html).not.toContain('<script>');
  findingDrafts.clear('p:s:finding-mechanical');
});

it('isolates mechanical preview selection and invalidation across a refreshed head/scope', async () => {
  const preview = new Preview();
  let older!: (value: string) => void, newer!: (value: string) => void;
  let shown = '';
  const show = async (scope: string, input: Promise<string>) => {
    const current = preview.begin(scope),
      value = await input;
    if (current()) shown = value;
  };
  const a = show(
    'p/s/f:1',
    new Promise((resolve) => {
      older = resolve;
    }),
  );
  const b = show(
    'p/s/g:2',
    new Promise((resolve) => {
      newer = resolve;
    }),
  );
  newer('current evidence');
  await b;
  older('old evidence');
  await a;
  expect(shown).toBe('current evidence');
  const pending = show(
    'p/s/g:2',
    new Promise((resolve) => {
      older = resolve;
    }),
  );
  preview.invalidate();
  older('previous head/session');
  await pending;
  expect(shown).toBe('current evidence');
});

it('uses scoped read-only mechanical evidence routes and preserves explicit unavailable/held state', async () => {
  const body = {
    checks: [{ check_id: 'math-x', state: 'finished', unavailable: 32 }],
    queue: [
      { document: 'doc-x', target: 2, completed: 2, running: 1, blocked: 'history quota reached' },
    ],
    next_after: null,
  };
  const fetch = vi.fn(
    async (_path: string, _init?: RequestInit) => new Response(JSON.stringify(body)),
  );
  vi.stubGlobal('fetch', fetch);
  const result = await reviews.math('project/one', 'session/two');
  expect(result.queue[0]?.running).toBe(1);
  expect(result.queue[0]?.blocked).toBe('history quota reached');
  expect(result.checks[0]?.unavailable).toBe(32);
  expect(fetch.mock.calls[0]?.[0]).toBe(
    '/api/v0/projects/project%2Fone/sessions/session%2Ftwo/math-checks',
  );
  await reviews.mathEvidence('p', 's', 'math/exact', 32, 'm1');
  expect(fetch.mock.calls[1]?.[0]).toBe(
    '/api/v0/projects/p/sessions/s/math-checks/math%2Fexact?after=32&span=m1',
  );
  expect(fetch.mock.calls[1]?.[1]?.method ?? 'GET').toBe('GET');
});
