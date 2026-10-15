// SPDX-License-Identifier: MIT
/**
 * The user's definition of a frontier category, or its revision at the revision read
 * (docs/specs/ledger.md, LG-18; docs/LEDGER.md, "Frontier categories"): a draft as the
 * form holds it, why the server would refuse it, the body that is sent, the exact summary
 * the user confirms, what a revision changes from the one read, and what a conflict means.
 */
import { ApiError } from './api';
import type { Category, CategoryMeasure } from './api-types';
import { CATEGORY_NAME } from './router';

/** The most measures and condition keys a category names, and the longest texts. */
export const MAX_MEASURES = 16;
export const MAX_CONDITIONS = 16;
export const MAX_NOTE = 4096;
export const MAX_UNIT = 512;

export interface MeasureDraft {
  name: string;
  unit: string;
  direction: CategoryMeasure['direction'];
}

export interface CategoryDraft {
  category: string;
  target: string;
  kind: string;
  measures: MeasureDraft[];
  conditions: string[];
  reason: string;
}

/** A new category's draft: one measure, no condition keys. */
export function emptyDraft(): CategoryDraft {
  return {
    category: '',
    target: '',
    kind: '',
    measures: [{ name: '', unit: '', direction: 'lower' }],
    conditions: [],
    reason: '',
  };
}

/** A draft of a revision of `category`, as it reads, with no reason yet. */
export function draftOf(category: Category): CategoryDraft {
  return {
    category: category.category,
    target: category.target,
    kind: category.kind,
    measures: category.measures.map((m) => ({ ...m })),
    conditions: [...category.conditions],
    reason: '',
  };
}

/** What is sent: the draft's names and keys trimmed, its texts as written. */
export interface CategoryBody {
  category: string;
  /** The revision read, to revise it; absent to define a new category. */
  expected?: number;
  target: string;
  kind: string;
  measures: CategoryMeasure[];
  conditions: string[];
  reason: string;
}

export function definitionBody(draft: CategoryDraft, base: Category | null): CategoryBody {
  return {
    category: draft.category.trim(),
    ...(base === null ? {} : { expected: base.revision }),
    target: draft.target,
    kind: draft.kind,
    measures: draft.measures.map((m) => ({
      name: m.name.trim(),
      unit: m.unit,
      direction: m.direction,
    })),
    // A blank row names no key.
    conditions: draft.conditions.map((key) => key.trim()).filter((key) => key !== ''),
    reason: draft.reason,
  };
}

const NAME_RULE = 'a letter or digit, then letters, digits, dots, dashes or underscores';

/** Why the server would refuse the body, in words; null when it would take it. */
export function formProblem(body: CategoryBody): string | null {
  if (!CATEGORY_NAME.test(body.category)) return `The category's name is ${NAME_RULE}.`;
  for (const [text, what] of [
    [body.target, 'target'],
    [body.kind, 'kind of result'],
    [body.reason, 'reason'],
  ] as const) {
    if (!text.trim()) return `Give the category's ${what}.`;
    if (text.length > MAX_NOTE) return `The ${what} is longer than ${MAX_NOTE} characters.`;
  }
  if (body.measures.length === 0) return 'Name at least one measure.';
  if (body.measures.length > MAX_MEASURES) return `Name at most ${MAX_MEASURES} measures.`;
  for (const measure of body.measures) {
    if (!CATEGORY_NAME.test(measure.name)) return `A measure's name is ${NAME_RULE}.`;
    if (!measure.unit.trim()) return `Give measure ${measure.name}'s unit.`;
    if (measure.unit.length > MAX_UNIT) {
      return `Measure ${measure.name}'s unit is longer than ${MAX_UNIT} characters.`;
    }
  }
  if (body.conditions.length > MAX_CONDITIONS) {
    return `Name at most ${MAX_CONDITIONS} condition keys.`;
  }
  for (const key of body.conditions) {
    if (!CATEGORY_NAME.test(key)) return `A condition key is ${NAME_RULE}.`;
  }
  const names = body.measures.map((m) => m.name);
  if (new Set(names).size !== names.length) return 'Name each measure once.';
  // The server compares condition keys case folded: `Model` is the key `model`.
  const keys = body.conditions.map((key) => key.toLowerCase());
  if (new Set(keys).size !== keys.length) {
    return 'Name each condition key once (keys are compared without case).';
  }
  return null;
}

const measureLine = (m: CategoryMeasure) => `${m.name}, in ${m.unit}, ${m.direction} is better`;

/** A category's parts as lines, under `head`, with its reason last. */
function lines(
  head: string,
  value: Pick<CategoryBody, 'target' | 'kind' | 'measures' | 'conditions'>,
  reason: string,
): string {
  return [
    head,
    `Target: ${value.target}`,
    `Kind of result: ${value.kind}`,
    'Measures:',
    ...value.measures.map((m) => `  ${measureLine(m)}`),
    `Condition keys: ${value.conditions.length ? value.conditions.join(', ') : 'none'}`,
    reason,
  ].join('\n');
}

/** Exactly what will be saved, as the user confirms it. */
export function summaryText(body: CategoryBody): string {
  const head =
    body.expected === undefined
      ? `Category: ${body.category} (new)`
      : `Category: ${body.category}, revising r${body.expected}`;
  return lines(head, body, `Reason: ${body.reason}`);
}

/** A category as it reads, in the summary's words, to set beside a draft. */
export function categoryText(category: Category): string {
  return lines(
    `Category: ${category.category}, r${category.revision}`,
    category,
    `Its reason: ${category.reason}`,
  );
}

/** What a revision changes from the one read, in words, as the frontier says it. */
export function changesText(base: Category, body: CategoryBody): string[] {
  const changed: string[] = [];
  if (base.target !== body.target) changed.push('the target');
  if (base.kind !== body.kind) changed.push('the kind of result');
  const old = new Map(base.measures.map((m) => [m.name, m]));
  const now = new Map(body.measures.map((m) => [m.name, m]));
  for (const [name, measure] of now) {
    const before = old.get(name);
    if (before === undefined) {
      changed.push(`measure ${name} added, ${measure.direction} is better`);
      continue;
    }
    if (before.unit !== measure.unit) changed.push(`measure ${name}'s unit`);
    if (before.direction !== measure.direction) {
      changed.push(`measure ${name}'s direction: ${measure.direction} is better`);
    }
  }
  for (const name of old.keys()) if (!now.has(name)) changed.push(`measure ${name} removed`);
  for (const key of body.conditions) {
    if (!base.conditions.includes(key)) changed.push(`condition key ${key} added`);
  }
  for (const key of base.conditions) {
    if (!body.conditions.includes(key)) changed.push(`condition key ${key} removed`);
  }
  return changed;
}

/**
 * What a refusal means for the draft, when it is a conflict: the category exists, or has
 * a newer revision than the one read; null for another failure.
 */
export function conflictText(error: unknown, body: CategoryBody): string | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const at = Number.isSafeInteger(error.details.revision)
    ? ` r${String(error.details.revision)}`
    : '';
  const what =
    body.expected === undefined
      ? `A category named ${body.category} exists already, at${at || ' a revision'}`
      : `${body.category} is at${at || ' a newer revision'} now, not the r${body.expected} you read`;
  return `${what}: nothing was saved. Your draft is kept; read the category as it is now, below.`;
}
