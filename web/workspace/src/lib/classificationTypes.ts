// SPDX-License-Identifier: MIT
/**
 * HAND-WRITTEN until the worker's classification contracts
 * (`sandbox_science.contracts.classifications`) are at this branch's base. At merge,
 * `scripts/generate_api_types.py` generates these into `api-types.ts`; then this file is
 * removed and its importers import from there. Field names follow the contracts' plan:
 * the workspace passes the worker's JSON through (`classifications.py`).
 */

export type EffectClass = 'read' | 'write' | 'communicate' | 'browse' | 'admin' | 'unclassified';
export type PinState = 'current' | 'changed' | 'added' | 'removed' | 'unpinned';
export type SignificantKind = 'url' | 'host' | 'recipient' | 'title' | 'size' | 'query';

export interface ArgumentRule {
  argument: string;
  present: true | null;
  equals: boolean | number | string | null;
  effect: EffectClass;
}

export interface ClassifiedTool {
  name: string;
  effect: EffectClass;
  reason: string;
  rules: ArgumentRule[];
  significant: Partial<Record<SignificantKind, string>> | null;
  sha256: string | null;
  pin_state: PinState;
  /** Whether the classification names the tool; else its class is its hints'. */
  mentioned: boolean;
  escalates: boolean;
}

export interface ClassificationSourceInfo {
  kind: 'catalog' | 'project';
  name: string | null;
  revision: number | null;
  pinned: string | null;
  accepted_at: string | null;
  accepted_by: string | null;
  reason: string | null;
}

export interface DigestedTool {
  name: string;
  sha256: string;
}

export interface DriftSummary {
  at: string;
  pin: string;
  added: DigestedTool[];
  removed: string[];
  changed: DigestedTool[];
  /** The middleware holds the server's non-read calls until re-accepted (D103). */
  held: boolean;
}

export interface ClassificationInForce {
  server: string;
  source: ClassificationSourceInfo;
  digest: string;
  tools: ClassifiedTool[];
  drift: DriftSummary | null;
}

export interface ClassificationView extends ClassificationInForce {
  project: string;
}

export interface ClassificationListing {
  project: string;
  classifications: ClassificationInForce[];
}

export interface DraftTool extends ClassifiedTool {
  current_effect: EffectClass | null;
  widening: boolean;
}

export interface ClassificationDraft {
  draft: string;
  server: string;
  created_at: string;
  model: string;
  yaml: string;
  tools: DraftTool[];
}

export interface DraftListing {
  project: string;
  server: string;
  drafts: ClassificationDraft[];
}

export interface ClassificationAccepted {
  revision: number;
  accepted_at: string;
  widenings: string[];
}

export interface DriftAccepted {
  accepted_at: string;
}
