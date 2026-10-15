// SPDX-License-Identifier: MIT
// The frontier as the view shows it: measures, marks and indicators in words, look-alike
// spellings told apart, groups once each, and views that render agents' text as text with
// every link leading into the workspace.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import FrontierCategory from '../views/FrontierCategory.svelte';
import FrontierGroup from '../views/FrontierGroup.svelte';
import type {
  Category,
  CategoryRevised,
  FrontierGroup as Group,
  FrontierResult,
} from './api-types';
import {
  conditionsText,
  indicatorsText,
  lookalikeText,
  measuresText,
  revisedSince,
  uniqueGroups,
  unverifiedText,
  windowText,
} from './frontier';
import { fieldRows } from './ledger';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';
const AGENT = { kind: 'agent', session_id: 's1', run_id: 'run-1', role: 'analyst' };

const category = (fields: Partial<Category> = {}): Category => ({
  category: 'key-recovery',
  revision: 2,
  head: 2,
  target: 'Kyber-768',
  kind: 'key recovery cost',
  measures: [
    { name: 'time', unit: 'bits', direction: 'lower' },
    { name: 'memory', unit: 'bits', direction: 'lower' },
  ],
  conditions: ['attack'],
  author: { kind: 'user', user: 'owner' },
  reason: 'compare attacks',
  operation_id: 'op-1',
  created: '2026-10-15T00:00:00Z',
  replayed: false,
  ...fields,
});

const result = (fields: Partial<FrontierResult> = {}): FrontierResult => ({
  entry_id: 'entry-0123456789abcdef01234567',
  handle: 'R-1',
  revision: 2,
  title: 'Primal attack',
  statement: 'Costs $2^{150}$.',
  status: 'established',
  origin: 'project',
  disputed: false,
  unverified: false,
  author: { kind: 'user', user: 'owner' },
  conditions: { attack: 'primal' },
  measures: { time: 150, memory: 90 },
  evidence_types: ['exact_computation'],
  sources: [],
  artifacts: [],
  open_findings: 0,
  open_flags: 0,
  dominated_by: [],
  linked_by: [],
  would_dominate: [],
  ...fields,
});

const group = (fields: Partial<Group> = {}): Group => ({
  conditions: { attack: 'primal' },
  key: JSON.stringify({ attack: 'primal' }),
  best: [result()],
  dominated: [],
  not_compared: [],
  candidates: [],
  more: {},
  ...fields,
});

/**
 * Every href the HTML holds, and whether its markup (not its text, which shows `<script>`
 * escaped) has an element or an attribute of its own that could run code.
 */
function inspect(html: string): { hrefs: string[]; hostile: boolean } {
  const hrefs = [...html.matchAll(/href="([^"]*)"/g)].map((match) => match[1]!);
  const hostile = [...html.matchAll(/<([a-zA-Z][\w-]*)([^>]*)>/g)].some(
    ([, tag, attributes]) =>
      /^(script|img|iframe|object|embed|svg|style)$/i.test(tag!) ||
      /\son[a-z]+\s*=|(?:href|src)\s*=\s*"?\s*(?:javascript|data|vbscript):/i.test(attributes!),
  );
  return { hrefs, hostile };
}

describe('the frontier in words', () => {
  it("gives each of the category's measures in its unit, as written, or missing", () => {
    const measures = category().measures;
    expect(measuresText(result(), measures)).toEqual(['time: 150 bits', 'memory: 90 bits']);
    expect(measuresText(result({ measures: { time: 'O(2^n)', old: 3 } }), measures)).toEqual([
      'time: O(2^n) (as written, not a number)',
      'memory: missing',
      'old: 3 (no longer a measure of the category)',
    ]);
  });

  it('names who wrote an unverified result, with the role, and the revision to confirm', () => {
    expect(unverifiedText(result({ author: AGENT, revision: 3 }))).toBe(
      'Unverified: published by analyst agent run run-1 of session s1, role analyst, and not ' +
        'confirmed by you as r3 reads, so it is a candidate.',
    );
  });

  it('gives the indicators, the evidence types in words', () => {
    const shown = result({
      evidence_types: ['proof', 'finite_test'],
      artifacts: [{ artifact_id: 'art-1', revision: 4 }],
      open_findings: 1,
      open_flags: 2,
    });
    expect(indicatorsText(shown)).toEqual([
      'Evidence: proof, finite test',
      'Artifacts: art-1 r4',
      '1 open finding, 2 open flags',
    ]);
    expect(indicatorsText(result({ evidence_types: [] }))[0]).toBe('No evidence');
  });

  it('says which candidates were read', () => {
    expect(windowText(null, false)).toBe('');
    expect(windowText(null, true)).toBe(
      'Candidates: of the proposed results, the 500 newest are read.',
    );
    expect(windowText(40, false)).toMatch(
      /^Candidates: the proposed results made before R-40, are/,
    );
  });

  it('tells look-alike spellings apart by their code points', () => {
    expect(lookalikeText({ attack: 'dual‐hybrid', model: 'cоre' })).toBe(
      'attack: dual‐hybrid (U+2010); model: cоre (U+043E)',
    );
    expect(lookalikeText({ attack: 'a​b' })).toBe('attack: a​b (U+200B)');
    expect(conditionsText({})).toBe('No conditions');
    // A heading, the history and the past show each hidden character as its code point.
    expect(conditionsText({ 'at\u200Btack': 'dual\u202Ehybrid', model: 'Gröbner' })).toBe(
      'at⟦U+200B⟧tack: dual⟦U+202E⟧hybrid; model: Gröbner',
    );
  });

  it('says what the main agent changed since the user, or that the user wrote none', () => {
    const revised: CategoryRevised = {
      revision: 3,
      created: '2026-10-15T00:00:00Z',
      author: AGENT,
      reason: 'r',
      since: 2,
      changed: ["measure time's direction: higher is better"],
    };
    expect(revisedSince(revised)).toBe('What changed since your r2:');
    expect(revisedSince({ ...revised, changed: [] })).toBe(
      'Nothing it compares changed since your r2.',
    );
    expect(revisedSince({ ...revised, since: null })).toBe('You have written no revision of it.');
  });

  it('lists the candidates as the answer gives them, newest first, and how many older are not', () => {
    // The route lists a group's newest 100 candidates, newest first.
    const many = Array.from({ length: 100 }, (_, n) =>
      result({ entry_id: `entry-${n}`, handle: `R-${130 - n}` }),
    );
    const listed = group({ best: [], candidates: many, more: { candidates: 30 } });
    const html = render(FrontierGroup, {
      props: {
        projectId: 'p1',
        group: listed,
        measures: category().measures,
        done: async () => {},
      },
    }).body;
    expect(html).toContain('And 30 older, not listed.');
    expect(html).not.toContain('oldest 100');
    expect(html.indexOf('>R-130<')).toBeLessThan(html.indexOf('>R-31<'));
  });

  it('keeps each group once, by its key, the first time it was read', () => {
    const first = group({ best: [] });
    // The same folded key, spelled by another result.
    const respelled = group({ conditions: { Attack: 'Primal' } });
    const dual = group({ conditions: { attack: 'dual' }, key: '{"attack":"dual"}' });
    const found = uniqueGroups([first, respelled, dual]);
    expect(found).toEqual([first, dual]);
  });

  it("leads a result's category field to its frontier, and only a name the server takes", () => {
    expect(fieldRows({ category: 'key-recovery' }, 'p 1')[0]!.href).toBe(
      '#/projects/p%201/frontier/key-recovery',
    );
    expect(fieldRows({ category: 'a/../b' }, 'p1')[0]!.href).toBeNull();
    expect(fieldRows({ category: HOSTILE }, 'p1')[0]!.href).toBeNull();
  });
});

describe('the frontier views', () => {
  it('render no text as HTML: the Markdown component is their only renderer', () => {
    const views = import.meta.glob('../views/Frontier*.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    });
    expect(Object.keys(views).length).toBeGreaterThanOrEqual(5);
    for (const [path, source] of Object.entries(views)) {
      expect(String(source), path).not.toContain('{@html');
    }
  });

  it("show an agent's hostile category and its revision as text", () => {
    const html = render(FrontierCategory, {
      props: {
        category: category({
          target: HOSTILE,
          kind: HOSTILE,
          reason: HOSTILE,
          conditions: [HOSTILE],
          measures: [{ name: HOSTILE, unit: HOSTILE, direction: 'higher' }],
          author: { kind: 'agent', role: HOSTILE, run_id: HOSTILE, session_id: HOSTILE },
        }),
        revised: {
          revision: 3,
          created: HOSTILE,
          author: { kind: 'agent', role: HOSTILE },
          reason: HOSTILE,
          since: 1,
          changed: [HOSTILE],
        },
        lookalike: [[{ [HOSTILE]: HOSTILE }, { attack: 'dual‐hybrid' }]],
      },
    }).body;
    const found = inspect(html);
    expect(found.hostile).toBe(false);
    expect(found.hrefs).toEqual([]);
    expect(html).toContain('The main agent revised this category in r3');
    expect(html).toContain('dual‐hybrid (U+2010)');
  });

  it('show hostile results as text, every link leading to an entry in the workspace', () => {
    const hostile = result({
      handle: 'javascript:alert(1)',
      title: HOSTILE,
      status: HOSTILE,
      origin: 'published',
      unverified: true,
      disputed: true,
      author: { kind: 'agent', role: HOSTILE },
      conditions: { attack: HOSTILE },
      measures: { time: HOSTILE },
      evidence_types: [HOSTILE],
      sources: ['REF-1', HOSTILE],
      artifacts: [{ artifact_id: HOSTILE, revision: HOSTILE }],
      dominated_by: ['R-2', 'R-1/../x'],
      would_dominate: [HOSTILE, 'R-3'],
      statement: `${HOSTILE}\n\n[a](javascript:alert(1))`,
    });
    const html = render(FrontierGroup, {
      props: {
        projectId: 'p1',
        group: group({
          conditions: { [HOSTILE]: HOSTILE },
          key: JSON.stringify({ [HOSTILE]: HOSTILE }),
          best: [],
          candidates: [result({ handle: 'R-9', entry_id: 'entry-9' }), hostile],
          more: { candidates: 4 },
        }),
        measures: category().measures,
        done: async () => {},
      },
    }).body;
    const found = inspect(html);
    expect(found.hostile).toBe(false);
    expect(found.hrefs).toEqual([
      '#/projects/p1/ledger/R-9/revisions/2',
      '#/projects/p1/ledger/REF-1',
      '#/projects/p1/ledger/R-2',
      '#/projects/p1/ledger/R-3',
    ]);
    expect(html).toContain('Unverified: published by');
    expect(html).toContain('Disputed: shown as r2');
    expect(html).toContain('And 4 older, not listed.');
    // As the answer lists them, newest first.
    expect(html.indexOf('R-9')).toBeLessThan(html.indexOf('javascript:alert(1)'));
  });
});
