// SPDX-License-Identifier: MIT
// Folds in the workspace (CM-15, CM-21, CM-46): a fold's proposals, framed as a model's
// words from folded material, with their anchors and fold linked; a run's folds and
// overviews with their levels, retired markers and the region's budget; and the chat's
// fold lines, each fold leading to its record. A model's text is shown as text.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import FoldProposalCard from '../views/FoldProposalCard.svelte';
import RunFolds from '../views/RunFolds.svelte';
import type { RunRecord } from './api';
import type { ProposalView } from './api-types';
import { hostileMarkup } from './approvalTesting';
import { chatEntries, foldText } from './chat';
import { proposalLine, runFolds } from './folds';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';
const FOLD = 'msg-00000000000000f1';

const proposal = (fields: Partial<ProposalView['proposal']> = {}): ProposalView => ({
  proposal_id: 'prop-0123456789abcdef01234567',
  session: 's1',
  fold: FOLD,
  run: 'run-1',
  created: '2026-10-16T00:00:00Z',
  proposal: {
    op: 'decision',
    title: `Drop the sieve ${HOSTILE}`,
    statement: 'The sieve​ is too slow.',
    kind: null,
    concerns: [],
    status: 'rejected',
    reason: 'It ran out of memory.',
    reopen_condition: 'none',
    reopen_on: [],
    entry: null,
    tried: null,
    limit: null,
    caveat: null,
    relied_on_by: [],
    basis: 'observed',
    anchors: [],
    ...fields,
  },
  anchors: [{ message: 'msg-0123456789abcdef', kind: 'tool_result', start: 0, end: 40 }],
  state: 'waiting',
  result: null,
  reason: null,
  decided_by: null,
  decided_at: null,
});

describe("a fold's proposals", () => {
  it('reads what it proposes, its basis, its words, its anchors and its fold', () => {
    const line = proposalLine('p1', proposal());
    expect(line.kind).toBe('A decision: an approach abandoned');
    expect(line.basis).toBe('observed: what a tool result, an artifact or a source shows');
    expect(line.fields[0]).toEqual(['Title', `Drop the sieve ${HOSTILE}`]);
    expect(line.fields[1]).toEqual(['Statement', 'The sieve⟦U+200B⟧ is too slow.']);
    expect(line.anchors[0]!.href).toBe('#/projects/p1/messages/msg-0123456789abcdef');
    expect(line.fold).toEqual({ text: FOLD, href: `#/projects/p1/messages/${FOLD}` });
    const issue = proposalLine('p1', proposal({ op: 'issue', kind: 'question', status: null }));
    expect(issue.kind).toBe('A new question issue');
    const work = proposalLine('p1', proposal({ op: 'work', entry: 'C-2', limit: 'time' }));
    expect(work.kind).toBe('A work record on C-2, ended at a limit (time)');
  });

  it('renders as a model’s words, as text, with Accept and Dismiss', () => {
    const html = render(FoldProposalCard, {
      props: {
        line: proposalLine('p1', proposal()),
        accept: async () => true,
        dismiss: async () => true,
      },
    }).body;
    expect(hostileMarkup(html)).toBe(false);
    expect(html).toContain("a fold's model, from folded messages");
    expect(html).toContain(`href="#/projects/p1/messages/${FOLD}"`);
    expect(html).toMatch(/>Accept</);
    expect(html).toMatch(/>Dismiss…</);
  });
});

describe("a run's folds", () => {
  const run: Pick<RunRecord, 'summaries' | 'context'> = {
    context: {
      project_id: 'p1',
      session_id: 's1',
      environment_id: 'e',
      policy_revision: null,
      actor: 'a',
    },
    summaries: {
      folds: {
        folds: [
          {
            fold: FOLD,
            level: 1,
            span: [1, 40],
            state: 'applied',
            freed: 12000,
            stubs: 3,
            proposals: { kept: 2 },
          },
          {
            fold: 'msg-00000000000000f2',
            level: 2,
            span: [1, 90],
            state: 'code_merge',
            freed: 800,
            stubs: 0,
            parts: [FOLD, 'msg-00000000000000f3'],
            reason: `built by code: ${HOSTILE}`,
          },
        ],
        rebuilds: [{ retired: {} }, { retired: { [FOLD]: [0, 2], 'msg-00000000000000f3': [] } }],
        region_over_budget: 1,
        weak_folds: 0,
      },
    },
  };

  it('lists each fold and overview with its level, its record, retired markers and the budget', () => {
    const found = runFolds('p1', run)!;
    expect(found.folds.map((fold) => fold.text)).toEqual([
      'Folded positions 1 to 40, with its narrative: about 12,000 tokens freed, 3 results stubbed. It proposed 2 research-state updates for the user to accept or dismiss.',
      `Overview (level 2) of positions 1 to 90, built by code: about 800 tokens freed, 0 results stubbed. The narrative failed: ${HOSTILE}.`,
    ]);
    expect(found.folds[0]!.href).toBe(`#/projects/p1/messages/${FOLD}`);
    expect(found.retired).toEqual([
      { text: FOLD, href: `#/projects/p1/messages/${FOLD}`, count: 2 },
    ]);
    expect(found.overs).toBe(1);
    expect(runFolds('p1', { summaries: {} })).toBeNull();
  });

  it('renders them as text', () => {
    const html = render(RunFolds, { props: { run } }).body;
    expect(hostileMarkup(html)).toBe(false);
    expect(html).toContain('the folded region went over its budget 1');
    expect(html).toContain('>Its record</a>');
    expect(render(RunFolds, { props: { run: { ...run, summaries: {} } } }).body).not.toContain(
      'Folds',
    );
  });
});

describe('fold events in the chat', () => {
  it('names overviews by level, the region’s budget, and links each fold to its record', () => {
    expect(foldText({ state: 'started', level: 2, span: [1, 90], parts: ['a', 'b'] })).toBe(
      'An overview (level 2) of positions 1 to 90 started; its narrative is being written.',
    );
    expect(foldText({ state: 'region_over_budget', chars: 90000, budget_tokens: 20000 })).toBe(
      'The folded region is over its budget of 20,000 tokens, with nothing left to merge.',
    );
    expect(foldText({ state: 'region_within_budget' })).toBe(
      'The folded region is back within its budget.',
    );
    const entries = chatEntries([
      {
        seq: 1,
        pins: [],
        at: '2026-10-16T00:00:00Z',
        kind: 'fold',
        data: { fold: FOLD, state: 'applied', span: [1, 40], freed: 10, stubs: 0 },
      },
      {
        seq: 2,
        pins: [],
        at: '2026-10-16T00:00:01Z',
        kind: 'fold',
        data: { fold: 'javascript:alert(1)', state: 'region_over_budget' },
      },
    ]);
    expect(entries.map((e) => (e.kind === 'note' ? [e.message, e.problem] : null))).toEqual([
      [FOLD, false],
      [undefined, true],
    ]);
  });
});
