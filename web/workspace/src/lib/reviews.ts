// SPDX-License-Identifier: MIT
/** Review text and pinned evidence are untrusted data, never browser HTML. */
import { get, post, type Json } from './api';
export interface Review { review_id: string; kind: string; state: string; summary: string;
  created: number; alias: string | null; resolved_model: string | null; rubric: string;
  calls: number; input_tokens: number; output_tokens: number; estimated_tokens: number;
  cap_reached: boolean | number; coverage_complete: boolean | number;
  start_seq: number; end_seq: number; config_revision: string; classifier: Json }
export interface Evidence { evidence: string; ordinal: number; kind: string;
  locator: Record<string, Json>; sha256: string; complete: boolean }
export interface Finding { finding_id: string; review_id: string; severity: string; category: string;
  statement: string; remedy: string | null; status: string; revision: number; reflags: number;
  anchors: ({ evidence: string; review: string; locator: Record<string, Json> } & Record<string, Json>)[];
  history?: Transition[]; history_before?: number | null }
export interface Transition { revision: number; status: string; actor: string; reason: string;
  payload: Json; created: number }
const part = encodeURIComponent;
const base = (project: string, session: string) => `/projects/${part(project)}/sessions/${part(session)}`;
export const reviews = {
  list: (p: string, s: string, after: string | null = null) => get<{ reviews: Review[]; next_after: string | null; unreviewed: Json[] }>(`${base(p,s)}/reviews${after ? `?after=${part(after)}` : ''}`),
  findings: (p: string, s: string, after: string | null = null) => get<{ findings: Finding[]; next_after: string | null }>(`${base(p,s)}/findings${after ? `?after=${part(after)}` : ''}`),
  manifest: (p: string, s: string, r: string, after = 0) => get<{ review: Review; evidence: Evidence[]; next_after: number | null }>(`${base(p,s)}/reviews/${part(r)}?after=${after}`),
  records: (p: string, s: string, r: string, after = 0) => get<{ records: { sequence: number; kind: string; payload: Json; created: number }[]; next_after: number | null }>(`${base(p,s)}/reviews/${part(r)}/records?after=${after}`),
  evidence: (p: string, s: string, r: string, e: string, start = 0) => get<{ text: string; next_start: number | null; locator: Json; complete: boolean }>(`${base(p,s)}/reviews/${part(r)}/evidence/${part(e)}?start=${start}`),
  finding: (p: string, s: string, f: string, before: number | null = null) => get<Finding>(`${base(p,s)}/findings/${part(f)}${before ? `?before=${before}` : ''}`),
  respond: (p: string, s: string, f: string, body: Record<string, unknown>) => post<{ revision: number; status: string }>(`${base(p,s)}/findings/${part(f)}`, body),
  request: (p: string, s: string) => post<{ queued: boolean; detail: string }>(`${base(p,s)}/reviews`, {}),
};
export function parseRevisionReferences(value: string): [string, number][] {
  if (!value.trim()) return [];
  const values = value.trim().split(/\s+/);
  if (values.length > 16) throw new Error('Use at most 16 changed revision references.');
  return values.map(item => {
    const match = /^(doc-[0-9a-f]{24}):([1-9][0-9]{0,8})$/.exec(item);
    if (!match) throw new Error('Changed revisions use document-ID:revision, separated by spaces.');
    return [match[1]!, Number(match[2]!)];
  });
}
