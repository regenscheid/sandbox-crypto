// SPDX-License-Identifier: MIT
// The usage view's logic (D102): caps against use, cost wording when some calls were
// unpriced, the order of agents, and how a check-in and its answer read.
import { describe, expect, it } from 'vitest';
import {
  agentDepth,
  agentTitle,
  answeredText,
  awaiting,
  capAmount,
  capShare,
  capState,
  capText,
  checkInOutcome,
  checkInReason,
  checkInSummary,
  checkInUse,
  durationText,
  joinCheckIns,
  limitText,
  loopLines,
  modelText,
  orderedAgents,
  ruleText,
  spendText,
  strained,
  strainedCount,
  strainText,
} from './usage';
import { agent, checkIn, counts, LOOP, root, ROOT, sessionView } from './usageTesting';

describe('caps', () => {
  it('state a cap as off, well inside, near or reached', () => {
    const cap = (used: number, total: number | null) =>
      capState({ resource: 'tokens', used, total });
    expect(cap(5, null)).toBe('off');
    expect(cap(5, 0)).toBe('off');
    expect(cap(79, 100)).toBe('ok');
    expect(cap(80, 100)).toBe('near');
    expect(cap(100, 100)).toBe('reached');
    expect(cap(250, 100)).toBe('reached'); // a soft cap passes: it is a check-in, not a stop
  });

  it('size a bar from 0 to 100 and none without a cap', () => {
    expect(capShare({ resource: 'turns', used: 150, total: 300 })).toBe(50);
    expect(capShare({ resource: 'turns', used: 900, total: 300 })).toBe(100);
    expect(capShare({ resource: 'turns', used: 4, total: null })).toBe(0);
  });

  it('write each unit: counts, time, and cost kept in hundredths', () => {
    expect(capAmount('tokens', 20_000_000)).toBe((20_000_000).toLocaleString());
    expect(capAmount('elapsed_seconds', 60)).toBe('60 s');
    expect(capAmount('elapsed_seconds', 3600)).toBe('60 min');
    expect(capAmount('elapsed_seconds', 14_400)).toBe('4.0 h');
    expect(capAmount('cost', 2500)).toBe('$25.00');
    expect(capText({ resource: 'cost', used: 75, total: 2500 })).toBe('$0.75 of $25.00');
    expect(capText({ resource: 'cost', used: 75, total: null })).toBe(
      '$0.75, no soft cap recorded',
    );
    expect(durationText(89)).toBe('89 s');
    expect(durationText(90)).toBe('2 min');
  });

  it('name an agent that is at or near a cap', () => {
    expect(strained(agent())).toBe('reached'); // its turns are at the cap
    const quiet = root();
    expect(strained(quiet)).toBe('ok');
    expect(strained({ ...quiet, caps: [{ resource: 'tokens', used: 85, total: 100 }] })).toBe(
      'near',
    );
    expect(strained({ ...quiet, caps: [] })).toBe('ok');
    expect(strainedCount(sessionView())).toEqual({ near: 0, reached: 1 });
    expect(strainText({ near: 0, reached: 0 })).toBeNull();
    expect(strainText({ near: 0, reached: 1 })).toBe('1 agent is at a soft cap.');
    expect(strainText({ near: 2, reached: 3 })).toBe(
      '3 agents are at a soft cap, 2 within a fifth of one.',
    );
  });
});

describe('spend', () => {
  it('is never a zero where the gateway priced nothing', () => {
    expect(spendText(null, 0)).toBe('unknown');
    expect(spendText(null, 3)).toBe('unknown (3 unpriced)');
    expect(spendText(2.25, 0)).toBe('$2.25');
    expect(spendText(2.25, 1)).toBe('$2.25 (+1 unpriced)');
    expect(spendText(0, 0)).toBe('$0');
  });

  it('show a model name with its hidden characters, or unknown', () => {
    expect(modelText(null)).toBe('unknown');
    expect(modelText('')).toBe('unknown');
    expect(modelText('gpt‮x')).toBe('gpt⟦U+202E⟧x');
  });

  it('say what a local limit leaves', () => {
    expect(limitText(null, null)).toBeNull();
    expect(limitText(10, null)).toContain('is set.');
    expect(limitText(10, 4)).toContain('$6.00 left');
    expect(limitText(10, 12)).toContain('set and spent');
  });
});

describe('agents', () => {
  const child = agent();
  const other = agent({ key: 'w2', work: 'w2', parent: child.work, cost: 5, tokens: 10, task: '' });
  const agents = [root(), child, other];

  it('title the root, a child by its task, and a run without a work record', () => {
    expect(agentTitle(root())).toBe('Research agent');
    expect(agentTitle(child)).toBe('Verify finite-field claims');
    expect(agentTitle(other)).toBe('Subagent');
    expect(agentTitle(agent({ task: 'x'.repeat(200) })).length).toBe(90);
    expect(agentTitle(agent({ task: 'run‮ it' }))).toBe('run⟦U+202E⟧ it');
    expect(agentTitle(agent({ work: null, role: null, key: 'unattributed' }))).toBe(
      'Not tied to a run',
    );
    expect(agentTitle(agent({ work: null, role: null, key: 'some-run' }))).toBe('Run');
  });

  it('order as they began, or the costliest or heaviest first, ties as given', () => {
    expect(orderedAgents(agents, 'tree').map((item) => item.key)).toEqual([ROOT, child.key, 'w2']);
    expect(orderedAgents(agents, 'cost').map((item) => item.key)).toEqual(['w2', ROOT, child.key]);
    expect(orderedAgents(agents, 'tokens').map((item) => item.key)).toEqual([
      ROOT,
      child.key,
      'w2',
    ]);
    const tied = [agent({ key: 'a', cost: null }), agent({ key: 'b', cost: null })];
    expect(orderedAgents(tied, 'cost').map((item) => item.key)).toEqual(['a', 'b']);
    expect(agents.map((item) => item.key)).toEqual([ROOT, child.key, 'w2']); // not reordered in place
  });

  it('indent by the parents the list names, and survive a cycle', () => {
    expect(agentDepth(agents, agents[0]!)).toBe(0);
    expect(agentDepth(agents, child)).toBe(1);
    expect(agentDepth(agents, other)).toBe(2);
    const loop = [
      agent({ key: 'x', work: 'x', parent: 'y' }),
      agent({ key: 'y', work: 'y', parent: 'x' }),
    ];
    expect(agentDepth(loop, loop[0]!)).toBeLessThanOrEqual(20);
    expect(agentDepth([other], other)).toBe(0); // a parent the list lacks
  });
});

describe('check-ins', () => {
  it('say why an agent checked in', () => {
    expect(checkInReason(checkIn())).toBe('Reached its soft tokens cap');
    expect(checkInReason(checkIn({ resource: 'elapsed_seconds' }))).toBe(
      'Reached its soft active time cap',
    );
    expect(checkInReason(LOOP)).toContain('the same tool call, with the same arguments');
    expect(checkInReason(checkIn({ resource: 'turns', parent: null, step: 2000 }))).toBe(
      'Reached the turn limit',
    );
    expect(checkInReason(checkIn({ resource: 'retained_bytes' }))).toBe(
      'Reached retained artifact bytes',
    );
    expect(ruleText('odd‮')).toBe('odd⟦U+202E⟧');
    expect(ruleText(null)).toBe('a repeating pattern');
  });

  it('say what was used against the cap, or the repeats of a loop', () => {
    expect(checkInUse(checkIn({ resource: 'cost', used: 2500, total: 2500 }))).toBe(
      '$25.00 of $25.00',
    );
    expect(checkInUse(LOOP)).toBe('8 repeats');
    expect(checkInUse(checkIn({ resource: 'retention_revisions', used: 3, total: 5 }))).toBe(
      '3 of 5 retained revisions',
    );
  });

  it('say how a check-in was answered, and by whom', () => {
    expect(checkInOutcome(checkIn())).toBe('Awaiting a decision');
    expect(awaiting(checkIn())).toBe(true);
    expect(checkInOutcome(checkIn({ work_state: 'stopped' }))).toBe(
      'Not answered; the work stopped',
    );
    expect(awaiting(checkIn({ work_state: 'completed' }))).toBe(false);
    expect(checkInOutcome(checkIn({ superseded: true }))).toBe(
      'Not answered; a newer attempt was recorded',
    );
    expect(checkInOutcome(LOOP)).toBe('The parent agent redirected it');
    const decided = (patch: Record<string, unknown>, action = 'extend') =>
      checkIn({
        decision: {
          ...LOOP.decision!,
          request: { ...LOOP.decision!.request, action },
          ...patch,
        } as never,
      });
    expect(checkInOutcome(decided({ operator: 'user' }, 'stop'))).toBe('You stopped it');
    expect(checkInOutcome(decided({ operator: 'resume' }))).toBe(
      'You, by resuming let it go on for one more step',
    );
    expect(checkInOutcome(decided({ automatic: true }))).toBe(
      'The platform let it go on for one more step',
    );
    expect(checkInOutcome(decided({}, 'extend_tokens'))).toBe(
      'The parent agent extended its tokens',
    );
    expect(checkInOutcome({ ...decided({}), parent: null })).toBe(
      "The root's own policy let it go on for one more step",
    );
  });

  it('count them, with the loop detections by heuristic and who answered', () => {
    expect(checkInSummary(counts())).toBe('No check-ins recorded.');
    expect(checkInSummary(counts({ total: 1, waiting: 0 }))).toBe(
      '1 check-in, none awaiting a decision',
    );
    const found = counts({
      total: 5,
      waiting: 2,
      loops: { same_error: 1, same_call: 2, unknown: 1 },
      answered_by: { parent: 2, user: 1, none: 2 },
    });
    expect(checkInSummary(found)).toBe(
      '5 check-ins, 4 from a loop detection, 2 awaiting a decision',
    );
    expect(loopLines(found)).toEqual([
      expect.stringContaining('2 × the same tool call'),
      expect.stringContaining('1 × the same error'),
      expect.stringContaining('1 × a repeating pattern'),
    ]);
    expect(answeredText(found)).toBe('2 by the parent agent, 1 by you');
    expect(answeredText(counts({ answered_by: { none: 3 } }))).toBe('');
  });

  it('join two pages of them once each', () => {
    const a = checkIn({ request_id: 'a' });
    const b = checkIn({ request_id: 'b' });
    expect(
      joinCheckIns([a, b], [b, checkIn({ request_id: 'c' })]).map((item) => item.request_id),
    ).toEqual(['a', 'b', 'c']);
  });
});
