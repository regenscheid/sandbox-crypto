// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import LedgerGatePanel from '../views/LedgerGatePanel.svelte';
import type { GateClause, GateDecision, GateView } from './api-types';
import { CLAUSE_WORDS, decisionGroups, gateSummary } from './ledgerGate';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

const clause = (name: GateClause['clause'], holds: boolean, reason = 'why'): GateClause => ({
  clause: name,
  holds,
  reason,
});

const decision = (fields: Partial<GateDecision> = {}): GateDecision => ({
  entry_id: 'entry-d1',
  handle: 'D-1',
  title: 'Establish C-1',
  status: 'in_effect',
  revision: 1,
  author: { kind: 'user', user: 'owner' },
  ...fields,
});

const gate = (fields: Partial<GateView> = {}): GateView => ({
  entry_id: 'entry-c1',
  handle: 'C-1',
  revision: 3,
  status: 'proposed',
  applies: true,
  holds: false,
  clauses: [
    clause('evidence', true),
    clause('independent_check', true),
    clause('computation', false),
    clause('review', false),
    clause('finding', true),
    clause('flags', false),
  ],
  decisions: [],
  ...fields,
});

/** Whether the markup (not its escaped text) has an element or attribute that could run code. */
function hostile(html: string): boolean {
  return [...html.matchAll(/<([a-zA-Z][\w-]*)([^>]*)>/g)].some(
    ([, tag, attributes]) =>
      /^(script|img|iframe|object|embed|svg|style)$/i.test(tag!) ||
      /\son[a-z]+\s*=|(?:href|src)\s*=\s*"?\s*(?:javascript|data|vbscript):/i.test(attributes!),
  );
}

describe('the gate', () => {
  it('says how much of it holds, and that the user needs none', () => {
    expect(gateSummary(gate())).toBe(
      '3 of 6 clauses hold: the main agent cannot establish r3 yet. Your own decision needs no gate.',
    );
    expect(gateSummary(gate({ holds: true }))).toBe(
      "Every clause holds: the main agent's decision could establish r3.",
    );
    expect(gateSummary(gate({ status: 'established' }))).toMatch(/^It is established/);
    expect(gateSummary(gate({ applies: false, clauses: [] }))).toBe(
      'The gate applies only to a claim or result of this project: this entry needs none.',
    );
  });

  it('names each clause the read gives, in words', () => {
    expect(Object.keys(CLAUSE_WORDS)).toEqual([
      'evidence',
      'independent_check',
      'computation',
      'review',
      'finding',
      'flags',
    ]);
  });

  it('lists the decisions in effect first, then the others, each in the order read', () => {
    const groups = decisionGroups([
      decision({ entry_id: 'a', status: 'proposed' }),
      decision({ entry_id: 'b' }),
      decision({ entry_id: 'c', status: 'reversed' }),
      decision({ entry_id: 'd' }),
    ]);
    expect(groups.inEffect.map((d) => d.entry_id)).toEqual(['b', 'd']);
    expect(groups.others.map((d) => d.entry_id)).toEqual(['a', 'c']);
  });
});

describe('the gate panel', () => {
  it('shows hostile reasons, titles and authors as text, every link in the workspace', () => {
    const html = render(LedgerGatePanel, {
      props: {
        projectId: 'p1',
        shown: 3,
        gate: gate({
          clauses: [clause('evidence', false, HOSTILE), clause('flags', true, HOSTILE)],
          decisions: [
            decision({
              title: HOSTILE,
              author: { kind: 'agent', session_id: HOSTILE, run_id: HOSTILE, role: 'main' },
            }),
            decision({ entry_id: 'e2', handle: 'javascript:alert(1)', status: HOSTILE }),
          ],
        }),
      },
    }).body;
    expect(hostile(html)).toBe(false);
    const hrefs = [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]!);
    expect(hrefs).toEqual(['#/projects/p1/ledger/D-1']);
    expect(html).toContain('&lt;script>');
    expect(html).toContain('Decisions in effect');
    expect(html).toContain('Other decisions that affect it');
  });

  it('says when it is read on a later revision than the one shown', () => {
    const html = render(LedgerGatePanel, {
      props: { projectId: 'p1', shown: 2, gate: gate() },
    }).body;
    expect(html).toContain('Read on the entry as it is now, r3, not the r2 shown.');
  });

  it('shows no clause where the gate does not apply, and still the decisions', () => {
    const html = render(LedgerGatePanel, {
      props: {
        projectId: 'p1',
        shown: 3,
        gate: gate({ applies: false, clauses: [], decisions: [decision({ status: 'proposed' })] }),
      },
    }).body;
    expect(html).not.toContain('Not yet');
    expect(html).toContain('D-1');
  });
});
