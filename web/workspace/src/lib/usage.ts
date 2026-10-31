// SPDX-License-Identifier: MIT
/**
 * The usage view's logic (D102): soft caps against what an agent has used, how a cost is
 * said when the gateway priced some calls and not others, and how a check-in and its
 * answer read. A soft cap is a check-in, never a stop, so nothing here is worded as a
 * limit that was enforced. Agents' text (a task, a check-in's detail or a decision's
 * reason) shows each hidden character as its code point.
 */
import type {
  AgentUsage,
  CapUse,
  CheckInCounts,
  ResourceRequest,
  SessionUsageView,
} from './api-types';
import { formatCost } from './format';
import { visible } from './text';

export type CapState = 'off' | 'ok' | 'near' | 'reached';

/** A cap's wording; "tokens" is the cumulative input plus output across attempts. */
export const CAP_NAMES: Record<CapUse['resource'], string> = {
  tokens: 'Tokens',
  turns: 'Turns',
  elapsed_seconds: 'Active time',
  cost: 'Cost',
};

/** The loop heuristics (resource `loop`), as the platform names their rule. */
const RULES: Record<string, string> = {
  same_call: 'the same tool call, with the same arguments, again and again',
  same_error: 'the same error, again and again',
  no_progress: 'many model calls in a row with nothing new produced',
  same_output: 'the same answer, several times in a row',
};

/** Seconds as "40 s", "12 min" or "3.5 h". */
export function durationText(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

/** An amount of one cap's unit: tokens, turns, time, or cost (kept in hundredths). */
export function capAmount(resource: CapUse['resource'], value: number): string {
  switch (resource) {
    case 'elapsed_seconds':
      return durationText(value);
    case 'cost':
      return formatCost(value / 100);
    default:
      return value.toLocaleString();
  }
}

/** Whether a cap is off (or not recorded), well inside, within a fifth of, or at its total. */
export function capState(cap: CapUse): CapState {
  if (cap.total === null || cap.total <= 0) return 'off';
  if (cap.used >= cap.total) return 'reached';
  return cap.used * 5 >= cap.total * 4 ? 'near' : 'ok';
}

/** The share of a cap used, 0 to 100, for a bar; 0 when there is no cap. */
export function capShare(cap: CapUse): number {
  if (cap.total === null || cap.total <= 0) return 0;
  return Math.min(100, (cap.used / cap.total) * 100);
}

/** "12,000 of 20,000,000", or, with no cap to measure against, the use alone. */
export function capText(cap: CapUse): string {
  const used = capAmount(cap.resource, cap.used);
  return cap.total === null || cap.total <= 0
    ? `${used}, no soft cap recorded`
    : `${used} of ${capAmount(cap.resource, cap.total)}`;
}

/** A cost the gateway may have priced only in part: "unknown", "$1.23", "$1.23 (+2 unpriced)". */
export function spendText(cost: number | null, unpriced: number): string {
  if (cost === null) return unpriced > 0 ? `unknown (${unpriced} unpriced)` : 'unknown';
  return unpriced > 0 ? `${formatCost(cost)} (+${unpriced} unpriced)` : formatCost(cost);
}

/** A model as the gateway named it, or "unknown" where the record names none. */
export function modelText(model: string | null): string {
  return model === null || model === '' ? 'unknown' : visible(model);
}

/** An agent's heading: the root, or a child by the task it was given. */
export function agentTitle(agent: AgentUsage): string {
  if (agent.role === 'main') return 'Research agent';
  if (agent.work === null) return agent.key === 'unattributed' ? 'Not tied to a run' : 'Run';
  const task = agent.task.replace(/\s+/g, ' ').trim();
  const shown = task.length > 90 ? `${task.slice(0, 89)}…` : task;
  return shown === '' ? 'Subagent' : visible(shown);
}

export type AgentOrder = 'tree' | 'cost' | 'tokens';

/** How deep an agent sits under the root, from the parents the agents list names. */
export function agentDepth(agents: readonly AgentUsage[], agent: AgentUsage): number {
  const byWork = new Map(agents.flatMap((item) => (item.work === null ? [] : [[item.work, item]])));
  let depth = 0;
  let parent = agent.parent;
  while (parent !== null && depth < 20) {
    const next = byWork.get(parent);
    if (next === undefined) break;
    depth += 1;
    parent = next.parent;
  }
  return depth;
}

/** The agents as given (the root, then the others as they began), or the costliest or
 * the heaviest in tokens first, ties kept in the given order. */
export function orderedAgents(agents: readonly AgentUsage[], order: AgentOrder): AgentUsage[] {
  const kept = [...agents];
  if (order === 'tree') return kept;
  const key = (agent: AgentUsage) => (order === 'cost' ? (agent.cost ?? 0) : agent.tokens);
  return kept
    .map((agent, index) => ({ agent, index }))
    .sort((a, b) => key(b.agent) - key(a.agent) || a.index - b.index)
    .map((item) => item.agent);
}

/** Whether any of an agent's caps is at its total or within a fifth of it. */
export function strained(agent: AgentUsage): CapState {
  const states = agent.caps.map(capState);
  if (states.includes('reached')) return 'reached';
  return states.includes('near') ? 'near' : 'ok';
}

/** A loop check-in's heuristic in words; a rule the platform does not name is shown as is. */
export function ruleText(rule: string | null | undefined): string {
  if (rule === null || rule === undefined || rule === '') return 'a repeating pattern';
  return RULES[rule] ?? visible(rule);
}

/** The loop detections of a scope, a line for each heuristic that tripped. */
export function loopLines(counts: CheckInCounts): string[] {
  return Object.entries(counts.loops)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([rule, count]) => `${count} × ${ruleText(rule === 'unknown' ? null : rule)}`);
}

const WHO: Record<string, string> = {
  user: 'you',
  parent: 'the parent agent',
  root: "the root's own policy",
  automatic: 'the platform (no parent answered in time, or it was the root checking itself)',
  none: 'nobody yet',
};

/** Who answered check-ins, as counts: "2 by the parent agent, 1 by you". */
export function answeredText(counts: CheckInCounts): string {
  const parts = Object.entries(counts.answered_by)
    .filter(([who]) => who !== 'none')
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([who, count]) => `${count} by ${WHO[who] ?? visible(who)}`);
  return parts.join(', ');
}

/** The one-line account of a scope's check-ins: how many, how many wait, how many were loops. */
export function checkInSummary(counts: CheckInCounts): string {
  if (counts.total === 0) return 'No check-ins recorded.';
  const loops = Object.values(counts.loops).reduce((sum, count) => sum + count, 0);
  const parts = [`${counts.total} check-in${counts.total === 1 ? '' : 's'}`];
  if (loops > 0) parts.push(`${loops} from a loop detection`);
  parts.push(
    counts.waiting > 0 ? `${counts.waiting} awaiting a decision` : 'none awaiting a decision',
  );
  return parts.join(', ');
}

const UNITS: Record<string, string> = {
  loop: 'repeats',
  retained_bytes: 'retained artifact bytes',
  retention_bytes: 'retention capacity bytes',
  retention_revisions: 'retained revisions',
  retention_operations: 'retention operations',
  retention_metadata_bytes: 'retention metadata bytes',
};

/** Why an agent checked in: the soft cap it reached, or the loop heuristic that tripped. */
export function checkInReason(item: ResourceRequest): string {
  if (item.resource === 'loop') return `Loop detection: ${ruleText(item.summary?.rule)}`;
  if (item.resource in CAP_NAMES) {
    const name = CAP_NAMES[item.resource as CapUse['resource']].toLowerCase();
    return item.resource === 'turns' && item.parent === null && item.step > 0
      ? 'Reached the turn limit'
      : `Reached its soft ${name} cap`;
  }
  return `Reached ${UNITS[item.resource] ?? item.resource}`;
}

/** What the agent had used at the check-in, against the cap that applied. */
export function checkInUse(item: ResourceRequest): string {
  if (item.resource in CAP_NAMES) {
    return capText({
      resource: item.resource as CapUse['resource'],
      used: item.used,
      total: item.total,
    });
  }
  const unit = UNITS[item.resource] ?? item.resource;
  return item.resource === 'loop'
    ? `${item.used.toLocaleString()} ${unit}`
    : `${item.used.toLocaleString()} of ${item.total.toLocaleString()} ${unit}`;
}

/** A check-in is still open when no one answered it and its work goes on. */
export function awaiting(item: ResourceRequest): boolean {
  return (
    item.decision === null &&
    !item.superseded &&
    item.work_state !== 'completed' &&
    item.work_state !== 'stopped'
  );
}

const ACTIONS: Record<string, string> = {
  extend: 'let it go on for one more step',
  redirect: 'redirected it',
  stop: 'stopped it',
  continue: 'let it continue',
  report: 'asked for its report',
  pause: 'paused it',
  extend_turns: 'extended its turns',
  extend_time: 'extended its time',
  extend_storage: 'extended its storage',
  extend_tokens: 'extended its tokens',
  extend_cost: 'extended its cost',
};

/** How a check-in was answered, in words: who, and what they did. */
export function checkInOutcome(item: ResourceRequest): string {
  const decision = item.decision;
  if (decision === null) {
    if (awaiting(item)) return 'Awaiting a decision';
    return item.superseded
      ? 'Not answered; a newer attempt was recorded'
      : `Not answered; the work ${item.work_state}`;
  }
  const who = decision.operator
    ? decision.operator === 'resume'
      ? 'You, by resuming'
      : 'You'
    : decision.automatic
      ? 'The platform'
      : item.parent
        ? 'The parent agent'
        : "The root's own policy";
  const action = decision.request.action;
  return `${who} ${ACTIONS[action] ?? visible(action.replaceAll('_', ' '))}`;
}

/** Two pages of check-ins joined, the newest first, each once. */
export function joinCheckIns(
  first: readonly ResourceRequest[],
  older: readonly ResourceRequest[],
): ResourceRequest[] {
  const seen = new Set<string>();
  return [...first, ...older].filter((item) => {
    if (seen.has(item.request_id)) return false;
    seen.add(item.request_id);
    return true;
  });
}

/** What the project's local cost limit leaves, as words; null when none is set. */
export function limitText(limit: number | null, lastDay: number | null): string | null {
  if (limit === null) return null;
  if (lastDay === null) return `A local daily cost limit of ${formatCost(limit)} is set.`;
  const left = limit - lastDay;
  return left >= 0
    ? `A local daily cost limit of ${formatCost(limit)} is set; ${formatCost(lastDay)} used in the last 24 hours, ${formatCost(left)} left.`
    : `A local daily cost limit of ${formatCost(limit)} is set and spent: ${formatCost(lastDay)} in the last 24 hours.`;
}

/** The view's whole-session account of its soft caps: how many agents are at or near one. */
export function strainedCount(view: SessionUsageView): { near: number; reached: number } {
  let near = 0;
  let reached = 0;
  for (const agent of view.agents) {
    const state = strained(agent);
    if (state === 'reached') reached += 1;
    else if (state === 'near') near += 1;
  }
  return { near, reached };
}

/** "1 agent is at a soft cap, 2 within a fifth of one."; null when none is. */
export function strainText(found: { near: number; reached: number }): string | null {
  const parts: string[] = [];
  if (found.reached) {
    parts.push(`${found.reached} agent${found.reached === 1 ? ' is' : 's are'} at a soft cap`);
  }
  if (found.near) parts.push(`${found.near} within a fifth of one`);
  return parts.length ? `${parts.join(', ')}.` : null;
}
