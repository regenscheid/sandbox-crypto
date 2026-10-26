// SPDX-License-Identifier: MIT
// The conversation's agent rows (B10, items 2 and 3), mounted: an agent message, sent or
// received, by name with its first line, opening to its whole text as the agent's words
// with hidden characters as code points; a child's outcome block, loading, then with its
// status, summary, deliverables, lessons and links; the run view's breadcrumb from the
// session down to a child of a child; and the Agents panel's card a link opens.
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AgentMessageRow from '../views/AgentMessageRow.svelte';
import ChatRun from '../views/ChatRun.svelte';
import ChildOutcome from '../views/ChildOutcome.svelte';
import DelegationCard from '../views/DelegationCard.svelte';
import RunEventsView from '../views/RunEventsView.svelte';
import SessionAgents from '../views/SessionAgents.svelte';
import {
  agentDirectory,
  MessageBodies,
  receivedRow,
  sentRow,
  type AgentMessageRow as Row,
} from './agentMessages';
import type { AgentMessageView, Delegation, RunEvent, RunTree, TreeRun } from './api-types';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

function show<P extends Record<string, any>>(component: Component<P>, props: P): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(component, { target, props }));
  flushSync();
  return target;
}

const HIDDEN = 'Use q = 2^8‮ <img src=x onerror=alert(1)> **bold**';
const SHOWN = 'Use q = 2^8⟦U+202E⟧ <img src=x onerror=alert(1)> **bold**';

const run = (name: string, fields: Partial<TreeRun> = {}): TreeRun => ({
  name,
  run_id: `id-${name}`,
  role: 'delegated',
  parent: 'root',
  delegation: null,
  started_at: '2026-10-25T10:00:00Z',
  state: 'ended',
  stopped: 'answered',
  kept: true,
  alias: null,
  limits: null,
  tool_mix: null,
  ...fields,
});

const delegation = (id: string, fields: Partial<Delegation> = {}): Delegation => ({
  delegation_id: id,
  parent: 'root',
  run: 'child',
  kind: 'task',
  brief: { task: `Task ${id}` },
  started_at: '2026-10-25T10:00:00Z',
  ended: null,
  stopped: null,
  report: null,
  ended_at: null,
  refused: null,
  acknowledged: null,
  result: null,
  errors: [],
  alias: null,
  models: [],
  read_entry: null,
  read_proofs: null,
  files: null,
  environment_id: null,
  environment: null,
  ...fields,
});

const tree: RunTree = {
  runs: [
    run('root', { role: 'main', parent: null, run_id: 'id-root' }),
    run('child', { delegation: 'dlg-a' }),
    run('grandchild', { parent: 'child', delegation: 'dlg-c', state: 'running', stopped: null }),
  ],
  delegations: [
    delegation('dlg-a', {
      brief: {
        task: 'Count the points‮\nof the curve.',
        deliverables: [
          { id: 'd1', title: 'Point table', kind: 'artifact', description: 'CSV' },
          { id: 'd2', title: 'Proof sketch', kind: 'file', description: '' },
          { id: 'd3', title: 'Bound', kind: 'answer', description: '' },
        ],
      },
      ended: 'returned',
      stopped: 'answered',
      result: {
        status: 'blocked',
        obstacle: 'deliverables not accounted for: d3 (Bound)',
        set_by: { status: 'platform', obstacle: 'platform' },
        child_status: 'complete',
        summary: 'Counted 2^8 points.\nThe table is in out/points.csv.',
        deliverables: [
          { id: 'd1', status: 'produced', path: 'out/points.csv', revision: 2 },
          { id: 'd2', status: 'not_produced', reason: 'no time‮' },
        ],
        lessons: ['The NIST server answered 429 after 20 requests‮'],
      },
    }),
    delegation('dlg-c', { run: 'grandchild', parent: 'child', brief: { task: 'Factor N.' } }),
  ],
  delegations_total: 2,
};

describe('an agent message row', () => {
  it("shows a sent message by name, its first line, and its text as the agent's words", () => {
    const row: Row = sentRow({
      id: 'c1',
      name: 'message_child',
      arguments: JSON.stringify({ delegation_id: 'dlg-a', text: `${HIDDEN}\nSecond line.` }),
    });
    Object.assign(row, { done: true, message: 'agm-1', state: 'delivered' });
    const target = show(AgentMessageRow, {
      rows: [row],
      agents: agentDirectory(tree, 'root', 'main'),
    });
    expect(target.querySelector('.agent-message-who')!.textContent!.replace(/\s+/g, ' ')).toBe(
      'Research agent → Count the points⟦U+202E⟧',
    );
    expect(target.querySelector('.agent-message-preview')!.textContent).toBe(SHOWN);
    expect(target.querySelector('.agent-message-state')!.textContent).toBe('delivered');
    expect(target.querySelector('pre.said')!.textContent).toBe(`${SHOWN}\nSecond line.`);
    expect(
      target.querySelector('.agent-message-label')!.textContent!.replace(/\s+/g, ' '),
    ).toContain('not the user');
    // Shown as text: no element from the agent's words, no Markdown rendered.
    expect(target.querySelector('img')).toBeNull();
    expect(target.querySelector('strong')).toBeNull();
    expect(target.querySelector('details')!.open).toBe(false);
  });

  it("reads a received message's text by its ID, and names its sender", async () => {
    const message: AgentMessageView = {
      message_id: 'agm-2',
      sender_run: 'child',
      sender_delegation: 'dlg-a',
      recipient_delegation: null,
      body: HIDDEN,
      created_at: '2026-10-25T10:00:00Z',
      delivered_run: 'root',
      references: [],
    };
    const read = vi.fn(async () => ({ messages: [message], next_after: 'agm-2', has_more: false }));
    const target = show(AgentMessageRow, {
      rows: [receivedRow('agm-2')],
      agents: agentDirectory(tree, 'root', 'main'),
      bodies: new MessageBodies(read),
    });
    expect(target.querySelector('.agent-message-preview')!.textContent).toBe('reading its text…');
    await settle();
    expect(read).toHaveBeenCalledTimes(1);
    expect(target.querySelector('.agent-message-from')!.textContent).toBe(
      'Count the points⟦U+202E⟧',
    );
    expect(target.querySelector('.agent-message-preview')!.textContent).toBe(SHOWN);
    expect(target.querySelector('pre.said')!.textContent).toBe(SHOWN);
    expect(target.querySelector('img')).toBeNull();
  });

  it('says so when a received message cannot be read', async () => {
    const target = show(AgentMessageRow, {
      rows: [receivedRow('agm-3')],
      agents: agentDirectory(tree, 'root', 'main'),
      bodies: new MessageBodies(async () => ({ messages: [], next_after: null, has_more: false })),
    });
    await settle();
    expect(target.querySelector('.agent-message-preview')!.textContent).toBe(
      'its text could not be read',
    );
  });
});

describe("a child's outcome block", () => {
  it('shows the ID while the tree has not listed the delegation', () => {
    const target = show(ChildOutcome, {
      projectId: 'p1',
      sessionId: 's1',
      delegationId: 'dlg-z‮',
      tree: null,
    });
    expect(target.querySelector('h3')!.textContent).toBe('Subagent dlg-z⟦U+202E⟧');
    expect(target.textContent).toContain('loading its outcome');
    expect(target.querySelector('a')!.getAttribute('href')).toBe(
      '#/projects/p1/sessions/s1/agents/dlg-z%E2%80%AE',
    );
  });

  it('shows its name, state, status, summary, deliverables, lessons and links', () => {
    const opened: string[] = [];
    const target = show(ChildOutcome, {
      projectId: 'p1',
      sessionId: 's1',
      delegationId: 'dlg-a',
      tree,
      showAgent: (id: string) => opened.push(id),
    });
    const text = target.textContent!.replace(/\s+/g, ' ');
    expect(target.querySelector('h3')!.textContent).toBe('Count the points⟦U+202E⟧');
    expect(text).toContain('It returned (answered).');
    expect(text).toContain('Status: blocked, set by the platform; the child said complete');
    // The summary's first line, opening to the whole.
    const summary = target.querySelector('details.outcome-summary')!;
    expect(summary.querySelector('summary')!.textContent).toBe('Counted 2^8 points.');
    expect(summary.querySelector('p')!.textContent).toBe(
      'Counted 2^8 points.\nThe table is in out/points.csv.',
    );
    expect([...target.querySelectorAll('.deliverables li')].map((li) => li.textContent)).toEqual([
      'd1 Point table · produced → out/points.csv@2',
      'd2 Proof sketch · not produced: no time⟦U+202E⟧',
      'd3 Bound · unaccounted',
    ]);
    expect(text).toContain('Its lessons, in its own words');
    expect(target.querySelector('.lessons li')!.textContent).toBe(
      'The NIST server answered 429 after 20 requests⟦U+202E⟧',
    );
    const links = [...target.querySelectorAll('a')].map((a) => [
      a.textContent,
      a.getAttribute('href'),
    ]);
    expect(links).toEqual([
      ['Its files', '#/projects/p1/files/s1/id-child'],
      ['Its transcript', '#/projects/p1/sessions/s1/runs/child'],
    ]);
    target.querySelector<HTMLButtonElement>('.outcome-links button')!.click();
    expect(opened).toEqual(['dlg-a']);
  });
});

describe('the run view', () => {
  it('shows a breadcrumb from the session down to a child of a child, with links', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url.endsWith('/tree')
          ? new Response(JSON.stringify(tree))
          : new Response(JSON.stringify({ state: 'active', events: [] })),
      ),
    );
    const target = show(RunEventsView, { projectId: 'p1', sessionId: 's1', run: 'grandchild' });
    await settle();
    const crumbs = target.querySelector('nav.agent-trail')!;
    expect(crumbs.textContent!.replace(/\s+/g, ' ').trim()).toBe(
      'Session › Research agent › Count the points⟦U+202E⟧ › Factor N.',
    );
    expect(
      [...crumbs.querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href')]),
    ).toEqual([
      ['Session', '#/projects/p1/sessions/s1'],
      ['Research agent', '#/projects/p1/sessions/s1/runs/root'],
      ['Count the points⟦U+202E⟧', '#/projects/p1/sessions/s1/runs/child'],
    ]);
    expect(crumbs.querySelector('[aria-current="page"]')!.textContent).toBe('Factor N.');
    expect(target.querySelector('h1')!.textContent).toBe('Factor N.');
    expect(target.textContent).toContain('Running.');
    expect(
      [...target.querySelectorAll('a')]
        .find((a) => a.textContent === 'Its delegation card')!
        .getAttribute('href'),
    ).toBe('#/projects/p1/sessions/s1/agents/dlg-c');
  });

  it('keeps the run name alone when the tree cannot be read', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url.endsWith('/tree')
          ? new Response('{}', { status: 500 })
          : new Response(JSON.stringify({ state: 'ended', events: [] })),
      ),
    );
    const target = show(RunEventsView, { projectId: 'p1', sessionId: 's1', run: 'r‮' });
    await settle();
    expect(target.querySelector('nav.agent-trail')!.textContent!.replace(/\s+/g, ' ').trim()).toBe(
      'Session › r⟦U+202E⟧',
    );
    expect(target.querySelector('h1')!.textContent).toBe('Run r⟦U+202E⟧');
  });
});

describe('the Agents panel and the delegation card', () => {
  it('lists children by name with state, and opens the card a link names', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('{}', { status: 500 })),
    );
    const target = show(SessionAgents, {
      projectId: 'p1',
      sessionId: 's1',
      tree,
      focus: 'dlg-a',
    });
    const names = [...target.querySelectorAll('.agent-heading strong')].map((n) => n.textContent);
    expect(names).toEqual(['Factor N.', 'Count the points⟦U+202E⟧', 'Research agent']);
    const grandchild = target.querySelector('[data-delegation="dlg-c"]')!;
    expect(grandchild.textContent).toContain('Delegated by Count the points⟦U+202E⟧');
    expect(grandchild.querySelector('.pill')!.textContent).toBe('running');
    const child = target.querySelector('[data-delegation="dlg-a"]')!;
    expect(child.querySelector('.pill')!.textContent).toBe('returned');
    expect(child.querySelector<HTMLDetailsElement>('details.agent-delegation')!.open).toBe(true);
    expect(
      target.querySelector<HTMLDetailsElement>('[data-delegation="dlg-c"] details')!.open,
    ).toBe(false);
  });

  it('shows the deliverables accounted for and the lessons as the child’s words', () => {
    const target = show(DelegationCard, {
      projectId: 'p1',
      sessionId: 's1',
      delegation: tree.delegations[0]!,
    });
    const text = target.textContent!.replace(/\s+/g, ' ');
    expect(text).toContain('Its deliverables, as its result accounts for them');
    expect(text).toContain('d1 Point table · produced → out/points.csv@2');
    expect(text).toContain('d3 Bound · unaccounted');
    expect(text).toContain('Its lessons, in its own words');
    expect(text).toContain('The NIST server answered 429 after 20 requests⟦U+202E⟧');
    expect(text).toContain('Status: blocked, set by the platform; the child said complete');
  });
});

describe('the conversation', () => {
  it("shows a parent's message and its child's outcome among the run's rows", () => {
    const at = '2026-10-25T10:00:00Z';
    const events: RunEvent[] = [
      {
        seq: 1,
        at,
        pins: [],
        kind: 'assistant',
        data: {
          text: '',
          tool_calls: [
            {
              id: 'c1',
              name: 'message_child',
              arguments: JSON.stringify({ delegation_id: 'dlg-a', text: HIDDEN }),
            },
          ],
        },
      },
      { seq: 2, at, pins: [], kind: 'notice', data: { source: 'delegations', ids: ['dlg-a'] } },
    ];
    const target = show(ChatRun, {
      run: { run: 'root', state: 'ended', events },
      projectId: 'p1',
      sessionId: 's1',
      tree,
    });
    expect(target.querySelector('.agent-message-who')!.textContent!.replace(/\s+/g, ' ')).toBe(
      'Research agent → Count the points⟦U+202E⟧',
    );
    expect(target.querySelector('.child-outcome h3')!.textContent).toBe('Count the points⟦U+202E⟧');
    // The message is not also an ordinary tool row.
    expect(target.querySelector('.call-row')).toBeNull();
  });

  it('renders no text of the new rows as HTML', () => {
    const views = import.meta.glob(
      ['../views/AgentMessageRow.svelte', '../views/ChildOutcome.svelte'],
      { query: '?raw', import: 'default', eager: true },
    );
    expect(Object.keys(views)).toHaveLength(2);
    for (const source of Object.values(views)) expect(String(source)).not.toContain('{@html');
  });
});
