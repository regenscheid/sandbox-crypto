// SPDX-License-Identifier: MIT
/**
 * Inspected MCP traffic (docs/POLICY-MIDDLEWARE.md, "MCP traffic inspection"): the policy
 * middleware's record of each MCP call on the direct path, with its arguments, its answer's
 * excerpt, the patterns found in it and what the middleware decided. The workspace reads
 * the pages through its own route; the run page shows a run's calls, and each MCP tool
 * row of the chat the calls of its tool in its run, matched by arguments where they can
 * be. Everything in a record was written by the agent or a server: it is shown as text,
 * every hidden character a code point (`visible`).
 */
import { api } from './api';
import type { TrafficAnswer, TrafficCall } from './api-types';
import type { StepCall } from './conversation';
import { MAIN_ARGUMENT } from './conversation';
import { sizeText } from './pdfAnchors';

/** Pages one read follows at most, so that a long run's traffic does not read forever. */
export const MAX_PAGES = 5;

/** Tools of the harness's own, which reach no MCP server: their rows show no traffic. */
const HARNESS_TOOLS = new Set([
  ...Object.keys(MAIN_ARGUMENT),
  'mcp_tools',
  'ask_user',
  'plan_update',
  'delegate',
  'wait',
]);

/** Whether a chat row's call can be an MCP server's tool, whose traffic was inspected. */
export function mayBeConnector(call: Pick<StepCall, 'name'>): boolean {
  return !HARNESS_TOOLS.has(call.name);
}

export interface TrafficRead {
  calls: TrafficCall[];
  /** Why there is none, in a few words, when the worker could not say. */
  unavailable: string | null;
  /** More pages follow than one read follows. */
  more: boolean;
}

/** A run's (or a tool's) inspected calls, oldest first, over at most `MAX_PAGES` pages. */
export async function readTraffic(
  project: string,
  filter: { session?: string; run?: string; tool?: string },
  signal?: AbortSignal,
): Promise<TrafficRead> {
  const calls: TrafficCall[] = [];
  let after = 0;
  for (let page = 0; page < MAX_PAGES; page++) {
    const found = await api.mcpTraffic(project, { ...filter, after }, signal);
    if (found.traffic === null) {
      return { calls, unavailable: found.unavailable ?? 'unavailable', more: false };
    }
    calls.push(...found.traffic.calls);
    if (found.traffic.unavailable) {
      return { calls, unavailable: found.traffic.unavailable, more: false };
    }
    if (found.traffic.next_after === null) return { calls, unavailable: null, more: false };
    after = found.traffic.next_after;
  }
  return { calls, unavailable: null, more: true };
}

function same(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const left = a as Record<string, unknown>;
  const right = b as Record<string, unknown>;
  const keys = Object.keys(left);
  return (
    keys.length === Object.keys(right).length &&
    keys.every((key) => Object.hasOwn(right, key) && same(left[key], right[key]))
  );
}

function recordedArguments(call: TrafficCall): unknown {
  if (!call.arguments || call.arguments.truncated) return undefined;
  try {
    const params = JSON.parse(call.arguments.text) as Record<string, unknown> | null;
    return params && typeof params === 'object' ? params['arguments'] : undefined;
  } catch {
    return undefined;
  }
}

/**
 * The inspected calls a chat row's call made: those of its tool whose recorded arguments
 * are the row's, or, when none can be matched (arguments cut or redacted), every call of
 * its tool in the run, said so (`exact` false). Retries of a held call are each a call.
 */
export function callTraffic(
  calls: readonly TrafficCall[],
  call: Pick<StepCall, 'name' | 'arguments'>,
): { matched: TrafficCall[]; exact: boolean } {
  const ofTool = calls.filter((item) => item.method === 'tools/call' && item.tool === call.name);
  let wanted: unknown;
  try {
    wanted = JSON.parse(call.arguments);
  } catch {
    return { matched: ofTool, exact: false };
  }
  const matched = ofTool.filter((item) => same(recordedArguments(item), wanted));
  return matched.length ? { matched, exact: true } : { matched: ofTool, exact: false };
}

/** How the middleware decided a call, in words. */
export function callDecision(call: TrafficCall): string {
  const decision = call.decision;
  const by = [decision.action, decision.rule ? `rule ${decision.rule}` : null, decision.effect]
    .filter(Boolean)
    .join(', ');
  const said = decision.outcome === 'admitted' ? 'Admitted' : 'Refused';
  const code = decision.reason_code ? `: ${decision.reason_code}` : '';
  return `${said}${by ? ` (${by})` : ''}${code}`;
}

/** What inspection did with an answer, in words. */
export function answerDecision(answer: TrafficAnswer): string {
  const decision = answer.decision;
  if (decision.action === 'blocked')
    return `Refused whole, not delivered: ${decision.reason_code ?? decision.pattern ?? ''}`;
  if (decision.action === 'cut')
    return `Stream cut at the matching piece, an error sent instead: ${decision.reason_code ?? ''}`;
  return answer.findings.length ? 'Passed on as it came, with an alert' : 'Passed on as it came';
}

/** An answer's head in a line: status, media, size, how it was read. */
export function answerHead(answer: TrafficAnswer): string {
  const read = { whole: 'read whole', stream: 'read as it streamed', headers: 'head only' }[
    answer.mode
  ];
  const parts = [
    `${answer.status}`,
    answer.content_type ?? 'no content type',
    sizeText(answer.bytes),
    read,
    ...(answer.events !== null ? [`${answer.events} events`] : []),
    ...(answer.complete ? [] : ['ended early']),
  ];
  return parts.join(' · ');
}
