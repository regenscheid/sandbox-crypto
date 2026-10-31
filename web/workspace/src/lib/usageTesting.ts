// SPDX-License-Identifier: MIT
/** Answers of the usage view's routes, for the tests of its logic and its two views. */
import type {
  AgentUsage,
  CheckInCounts,
  ProjectUsageView,
  ResourceRequest,
  ScopeUsage,
  SessionUsageView,
} from './api-types';

export const ROOT = '20261017T000000000000Z-operator';
export const CHILD = '20261017T000001000000Z-check';

export const counts = (patch: Partial<CheckInCounts> = {}): CheckInCounts => ({
  total: 0,
  waiting: 0,
  by_resource: {},
  loops: {},
  answered_by: {},
  actions: {},
  ...patch,
});

export function scope(patch: Partial<ScopeUsage> = {}): ScopeUsage {
  return {
    session: null,
    run: null,
    cost: 2.25,
    tokens: 3300,
    calls: 4,
    unpriced: 1,
    by_role: [
      { role: 'main', cost: 1.5, tokens: 2200, calls: 2, unpriced: 1 },
      { role: 'delegated', cost: 0.5, tokens: 550, calls: 1, unpriced: 0 },
      { role: 'reviewer', cost: 0.25, tokens: 550, calls: 1, unpriced: 0 },
    ],
    ...patch,
  };
}

export function agent(patch: Partial<AgentUsage> = {}): AgentUsage {
  return {
    key: CHILD,
    work: CHILD,
    runs: [CHILD],
    parent: ROOT,
    role: 'delegated',
    state: 'running',
    reason: null,
    task: 'Verify finite-field claims',
    cost: 0.75,
    tokens: 1100,
    calls: 2,
    unpriced: 0,
    lines: [
      { role: 'delegated', model: 'small', cost: 0.5, tokens: 550, calls: 1, unpriced: 0 },
      { role: 'reviewer', model: 'judge', cost: 0.25, tokens: 550, calls: 1, unpriced: 0 },
    ],
    caps: [
      { resource: 'tokens', used: 17_000_000, total: 20_000_000 },
      { resource: 'turns', used: 300, total: 300 },
      { resource: 'elapsed_seconds', used: 600, total: 14_400 },
      { resource: 'cost', used: 75, total: null },
    ],
    ...patch,
  };
}

export function root(patch: Partial<AgentUsage> = {}): AgentUsage {
  return agent({
    key: ROOT,
    work: ROOT,
    runs: [ROOT],
    parent: null,
    role: 'main',
    task: '',
    cost: 1.5,
    tokens: 2200,
    calls: 2,
    unpriced: 1,
    lines: [{ role: 'main', model: 'big', cost: 1.5, tokens: 2200, calls: 2, unpriced: 1 }],
    caps: [
      { resource: 'tokens', used: 2200, total: 100_000_000 },
      { resource: 'turns', used: 2, total: null },
      { resource: 'elapsed_seconds', used: 60, total: 14_400 },
      { resource: 'cost', used: 150, total: 2500 },
    ],
    ...patch,
  });
}

export function checkIn(patch: Partial<ResourceRequest> = {}): ResourceRequest {
  return {
    request_id: 'resource-1',
    work: CHILD,
    current: CHILD,
    epoch: 1,
    parent: ROOT,
    resource: 'tokens',
    grant_revision: 0,
    used: 20_000_000,
    total: 20_000_000,
    checkpoint_message_id: null,
    created_at: 1_793_000_000,
    task: 'Verify finite-field claims',
    work_state: 'running',
    superseded: false,
    decision: null,
    scope: 'work',
    renewable: true,
    summary: null,
    step: 0,
    ...patch,
  };
}

export const LOOP = checkIn({
  request_id: 'resource-2',
  resource: 'loop',
  used: 8,
  total: 8,
  summary: {
    brief: '',
    produced: [],
    recent: [],
    answer: null,
    rule: 'same_call',
    detail: '8 calls of shell with the same arguments',
    tokens: 0,
    turns: 0,
    seconds: 0,
    cost: 0,
    progress: 0,
  },
  decision: {
    request: { request_id: 'resource-2', action: 'redirect', reason: 'Try a different approach' },
    parent_run: ROOT,
    operation: 'op-redirect',
    total: 8,
    observed: 1_793_000_100,
    operator: null,
    progress: 0,
    resource: 'loop',
    automatic: false,
  },
});

export function sessionView(patch: Partial<SessionUsageView> = {}): SessionUsageView {
  return {
    project: 'p1',
    session: 'a',
    at: '2026-10-31T12:00:00Z',
    total: scope({ session: 'a' }),
    by_model: [
      { model: 'big', cost: 1.5, tokens: 2200, calls: 2, unpriced: 1 },
      { model: 'small', cost: 0.5, tokens: 550, calls: 1, unpriced: 0 },
      { model: 'judge', cost: 0.25, tokens: 550, calls: 1, unpriced: 0 },
    ],
    agents: [root(), agent()],
    more_agents: 0,
    check_ins: { requests: [LOOP, checkIn()], next_after: null },
    counts: counts({
      total: 2,
      waiting: 1,
      by_resource: { loop: 1, tokens: 1 },
      loops: { same_call: 1 },
      answered_by: { parent: 1, none: 1 },
      actions: { redirect: 1 },
    }),
    daily_cost_limit: null,
    notices: [{ threshold: 1, crossed_at: '2026-10-31T11:00:00Z', spent: 2.25 }],
    ...patch,
  };
}

export function projectView(
  window: '24h' | '7d' | 'all',
  patch: Partial<ProjectUsageView> = {},
): ProjectUsageView {
  return {
    project: 'p1',
    window,
    since: window === 'all' ? null : '2026-10-30T12:00:00Z',
    at: '2026-10-31T12:00:00Z',
    total: scope(),
    all_time: scope({ cost: 9.5, tokens: 99_000, calls: 40 }),
    unassigned: scope({ cost: null, tokens: 0, calls: 0, unpriced: 0, by_role: [] }),
    by_model: [
      { model: 'big', cost: 1.5, tokens: 2200, calls: 2, unpriced: 1 },
      { model: null, cost: 0.75, tokens: 1100, calls: 2, unpriced: 0 },
    ],
    sessions: [
      {
        session: 'a',
        cost: 2,
        tokens: 3000,
        calls: 3,
        unpriced: 1,
        check_ins: 2,
        loops: 1,
        waiting: 1,
      },
      {
        session: 'b',
        cost: 0.25,
        tokens: 300,
        calls: 1,
        unpriced: 0,
        check_ins: 0,
        loops: 0,
        waiting: 0,
      },
    ],
    more_sessions: 0,
    counts: counts({
      total: 2,
      waiting: 1,
      loops: { same_call: 1, mystery: 1 },
      answered_by: { parent: 1, none: 1 },
    }),
    daily_cost_limit: null,
    last_day_cost: null,
    notices: [],
    ...patch,
  };
}
