// SPDX-License-Identifier: MIT
// What a permission request's card and the Grants panel say (D97): a structured summary of
// the request, never its payload, and the answers its ceiling allows. Every value is
// remote or agent text: the views show it with `visible`.
import type { PermissionDecision } from './api';
import type { PendingApproval, ProjectGrant } from './api-types';

export interface ScopeChoice {
  id: Exclude<PermissionDecision, 'deny' | 'clear' | 'stop'>;
  label: string;
  detail: string;
}

/** The card's question. */
export function cardTitle(request: PendingApproval): string {
  if (request.kind === 'network' && request.http)
    return `Let research code ${request.http.method} ${request.http.host}?`;
  if (request.kind === 'packages') {
    const one = request.packages?.package;
    return one ? `Download ${one}?` : 'Install these packages?';
  }
  return `Use ${request.tool}?`;
}

function sources(request: PendingApproval): string {
  const found = request.packages?.sources ?? [];
  return found.map((s) => (s.channel ? `${s.ecosystem} (${s.channel})` : s.ecosystem)).join(', ');
}

/** What a grant of this request would cover, in words. */
function covered(request: PendingApproval): string {
  if (request.kind === 'network') return `Research code's reads from ${request.http?.host ?? ''}`;
  if (request.kind === 'packages') return `Packages from ${sources(request)}`;
  return `Any ${request.effect ?? ''} call to ${request.server}`;
}

/** The answers the request's ceiling allows, narrowest first, as the menu offers them. */
export function scopeChoices(request: PendingApproval): ScopeChoice[] {
  const what = covered(request);
  const all: Record<string, ScopeChoice> = {
    once: { id: 'once', label: 'Once', detail: 'This request only' },
    session: { id: 'session', label: 'This session', detail: `${what}, in this session` },
    project: {
      id: 'project',
      label: 'This project',
      detail: `${what}, remembered for the project until you revoke it`,
    },
  };
  const offered = request.scopes.filter((scope) => scope in all).map((scope) => all[scope]!);
  return offered.length ? offered : [all['once']!];
}

/** The summary's rows: label and value. */
export function summaryRows(request: PendingApproval): [string, string][] {
  const rows: [string, string][] = [];
  if (request.kind === 'mcp') {
    rows.push(['Server', request.server]);
    if (request.effect) rows.push(['Effect', request.effect]);
    const names: Record<string, string> = {
      url: 'URL',
      host: 'Host',
      recipient: 'Recipient',
      title: 'Title',
      size: 'Size',
      query: 'Query',
    };
    for (const [kind, value] of Object.entries(request.summary))
      rows.push([names[kind] ?? kind, value]);
  } else if (request.http) {
    const http = request.http;
    rows.push(['Method', http.method], ['Host', `${http.host}:${http.port}`], ['Path', http.path]);
    if (http.content_type) rows.push(['Content type', http.content_type]);
    rows.push(['Size', `${http.size} bytes`]);
  }
  if (request.kind === 'packages' && request.packages) {
    const asked = request.packages;
    if (asked.asked) rows.push(['Asked for', asked.asked]);
    rows.push(['Sources', sources(request)], ['Files', String(asked.files)]);
  }
  return rows;
}

/** Who asked, in words. */
export function askedBy(request: PendingApproval): string {
  if (request.caller_kind === 'code') return 'research code';
  if (request.caller_kind === 'user') return 'you';
  return 'the agent';
}

const RULES: Record<string, string> = {
  'host-rate': 'a sustained burst of calls to one host',
  'host-spread': 'calls to very many hosts within minutes',
  'same-url': 'the same URL called again and again',
  'sequential-ids': 'URLs that step through numbers one by one',
  'host-failures': 'repeated failures from one host',
  'denial-streak': 'many refused calls in a short time',
  'daily-ceiling': "the project's daily ceiling of calls",
};

/** What a hold of the session's calls (D103) says it saw, in words. */
export function holdSummary(request: PendingApproval): string {
  const hold = request.hold;
  if (!hold) return 'A detection rule paused this session’s outward calls.';
  const what = RULES[hold.rule] ?? hold.rule;
  const where = hold.host ? ` to ${hold.host}` : '';
  const minutes =
    hold.window_seconds >= 120
      ? `${Math.round(hold.window_seconds / 60)} minutes`
      : `${hold.window_seconds} seconds`;
  return `Paused for ${what}: ${hold.count} calls${where} within ${minutes}, over the threshold of ${hold.threshold}.`;
}

/** A grant, in words: what it covers. */
export function grantLabel(grant: ProjectGrant): string {
  if (grant.kind === 'mcp') return `${grant.effect ?? ''} calls to ${grant.server ?? ''}`;
  if (grant.kind === 'network') return `Research code's reads from ${grant.host ?? ''}`;
  return grant.channel
    ? `Packages from ${grant.ecosystem ?? ''} (${grant.channel})`
    : `Packages from ${grant.ecosystem ?? ''}`;
}
