// SPDX-License-Identifier: MIT
/**
 * A project's skills as the skills page shows them (docs/specs/skills.md, SK-24): each
 * pin's import report read into its parts (the topics, entry skills and short descriptions
 * the import's model pass derived, marked as derived; the built-ins a pack leaves out;
 * hidden characters; broken links), and each run's skills from its record (what discovery
 * notices showed, and whether they matched by words or by meaning; what was loaded and
 * read). Pack text is untrusted: every string is shown with each character a person may
 * not see as its code point.
 */
import type { RunRecord } from './api';
import { visible } from './text';

const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const strings = (value: unknown): string[] =>
  list(value)
    .filter((item): item is string => typeof item === 'string')
    .map(visible);
const word = (value: unknown): string => (typeof value === 'string' ? visible(value) : '?');

/** What an import's model pass derived, every item marked as the pass's (SK-32). */
export interface Derived {
  by: string;
  topics: [string, string[]][];
  entry: string[];
  descriptions: [string, string][];
  bindings: [string, string][];
  /** Bindings not namespaced to the pack: held until a project's user approves them. */
  held: { variable: string; path: string; reason: string }[];
  dropped: number;
}

/** An import's report, read into the parts the page shows. */
export interface ReportView {
  skills: string[];
  derived: Derived | null;
  hidden: { path: string; count: number }[];
  broken: { path: string; link: string }[];
  warnings: string[];
  leftOut: string[];
  skipped: { path: string; reason: string }[];
}

/** An import's report; `project` names whose left-out built-ins to show. */
export function reportView(report: Record<string, unknown>, project: string): ReportView {
  const pass = record(report.derived);
  const derived: Derived | null = Object.keys(pass).length
    ? {
        by: `${word(pass.model)} (alias ${word(pass.alias)})${pass.cached === true ? ', cached' : ''}`,
        topics: Object.entries(record(pass.topics)).map(([name, skills]) => [
          visible(name),
          strings(skills),
        ]),
        entry: strings(pass.entry),
        descriptions: Object.entries(record(pass.descriptions)).map(([skill, text]) => [
          visible(skill),
          word(text),
        ]),
        bindings: Object.entries(record(pass.bindings)).map(([name, path]) => [
          visible(name),
          word(path),
        ]),
        held: list(pass.held).map((raw) => ({
          variable: word(record(raw).variable),
          path: word(record(raw).path),
          reason: word(record(raw).reason),
        })),
        dropped: list(pass.dropped).length,
      }
    : null;
  return {
    skills: strings(report.skills),
    derived,
    hidden: list(report.hidden_characters).map((raw) => {
      const item = record(raw);
      const count = typeof item.characters === 'number' ? item.characters : 0;
      return { path: word(item.path), count };
    }),
    broken: list(report.broken_links).map((raw) => ({
      path: word(record(raw).path),
      link: word(record(raw).link),
    })),
    warnings: list(report.warnings).map((raw) =>
      typeof raw === 'string' ? visible(raw) : visible(JSON.stringify(raw)),
    ),
    leftOut: strings(record(report.left_out)[project]),
    skipped: [...list(report.excluded), ...list(report.skipped)].map((raw) => ({
      path: word(record(raw).path),
      reason: word(record(raw).reason),
    })),
  };
}

/** How a discovery compared the text with the skills (lab 42): by words, or by meaning. */
export function scoringText(scoring: unknown): string {
  switch (scoring) {
    case 'lexical':
      return 'by words';
    case 'hybrid':
      return 'by words and meaning';
    case 'partial':
      return 'by words, and by meaning for some packs';
    case 'timed_out':
      return 'not compared: it timed out';
    case 'failed':
      return 'not compared: it failed';
    default:
      return word(scoring);
  }
}

/** One discovery: what it answered, how it matched, the skills it showed, and whether a
 * model call saw its notice. */
export interface Discovery {
  answered: string;
  scoring: string;
  skills: string[];
  entry: string[];
  seen: boolean | null;
}

/** One skill the run loaded or read, and how. */
export interface SkillUse {
  how: string;
  skill: string;
  detail: string;
}

/** A run's skills from its record: the discovery notices, and the skills it loaded and
 * read, at its start or with the `skills` tool. */
export function runSkills(run: Pick<RunRecord, 'tools' | 'summaries' | 'delivered'>): {
  discoveries: Discovery[];
  uses: SkillUse[];
} {
  const summary = record(run.summaries?.skills);
  const seen = strings(run.delivered?.skills);
  const ids = (value: unknown) => list(value).map((item) => word(record(item).id));
  const discoveries = list(summary.discoveries).map((raw): Discovery => {
    const item = record(raw);
    const notice = typeof item.notice === 'string' ? item.notice : null;
    return {
      answered: word(item.answered),
      scoring: scoringText(item.scoring),
      skills: ids(item.skills),
      entry: ids(item.entry),
      seen: notice === null ? null : seen.includes(visible(notice)),
    };
  });
  const uses: SkillUse[] = list(summary.loads).map((raw) => ({
    how: 'loaded at the start',
    skill: word(record(raw).id),
    detail: word(record(raw).status),
  }));
  for (const tool of run.tools) {
    const call = record(tool.skills);
    if (call.action === 'load') {
      for (const raw of list(call.loads)) {
        uses.push({ how: 'loaded', skill: word(record(raw).id), detail: word(record(raw).status) });
      }
    } else if (call.action === 'read') {
      uses.push({ how: 'read', skill: word(call.id), detail: word(call.path) });
    } else if (call.action === 'search') {
      uses.push({
        how: `searched, ${scoringText(call.scoring)}`,
        skill: strings(call.found).join(', ') || 'nothing found',
        detail: word(call.status),
      });
    }
  }
  return { discoveries, uses };
}

/** The user's overrides of what imports' passes derive (SK-33): each skill's fields, each
 * source's bindings taken away and approved, and its own prefix (SK-31). */
export function overridesView(overrides: Record<string, unknown>): {
  skills: { skill: string; fields: string[] }[];
  unbound: { source: string; variables: string[] }[];
  approved: { source: string; variables: string[] }[];
  prefixes: { source: string; prefix: string }[];
} {
  const skills = Object.entries(record(overrides.skills)).map(([skill, raw]) => ({
    skill: visible(skill),
    fields: Object.entries(record(raw)).map(([field, value]) =>
      field === 'entry'
        ? value === true
          ? 'an entry skill'
          : 'not an entry skill'
        : `${visible(field)}: ${typeof value === 'string' ? visible(value) : visible(JSON.stringify(value))}`,
    ),
  }));
  const bySource = (value: unknown) =>
    Object.entries(record(value)).map(([source, variables]) => ({
      source: visible(source),
      variables: strings(variables),
    }));
  const prefixes = Object.entries(record(overrides.prefixes)).map(([source, prefix]) => ({
    source: visible(source),
    prefix: word(prefix),
  }));
  return {
    skills,
    unbound: bySource(overrides.unbound),
    approved: bySource(overrides.approved),
    prefixes,
  };
}

/** Skill IDs, one a line, as the settings form takes them. */
export function idLines(text: string): string[] {
  return [
    ...new Set(
      text
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean),
    ),
  ];
}
