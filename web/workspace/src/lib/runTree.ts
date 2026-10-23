// SPDX-License-Identifier: MIT
/**
 * A session's run tree as the workspace shows it (docs/specs/plans-and-delegation.md,
 * PD-42): its runs nested under their parents, each with its role, state, model and the
 * limits it ran under; and each delegation's brief, read-only, its outcome and the files
 * its calls recorded (RA-36). The brief's note, the outcome's fields, the report and the
 * files' paths are agents' text, shown with each character a person may not see as its
 * code point.
 */
import type { Delegation, TreeRun } from './api-types';
import { sizeText } from './pdfAnchors';
import { visible } from './text';

/** A run with the runs it delegated to, in the order they started. */
export interface TreeNode {
  run: TreeRun;
  children: TreeNode[];
}

/**
 * The runs as a tree: each under its parent when the read lists the parent, the others
 * at the top, all in the order the read gives (oldest first). A parent cycle, which the
 * platform never makes, puts its runs at the top rather than losing them.
 */
export function runForest(runs: readonly TreeRun[]): TreeNode[] {
  const nodes = new Map(runs.map((run) => [run.name, { run, children: [] as TreeNode[] }]));
  const roots: TreeNode[] = [];
  for (const node of nodes.values()) {
    const parent = node.run.parent === null ? undefined : nodes.get(node.run.parent);
    if (parent === undefined || parent === node || reaches(nodes, parent.run.name, node.run.name)) {
      roots.push(node);
    } else {
      parent.children.push(node);
    }
  }
  return roots;
}

/** Whether `from`'s parents lead to `to`: adding `to` under `from` would close a cycle. */
function reaches(nodes: Map<string, TreeNode>, from: string, to: string): boolean {
  let at: string | null = from;
  for (let step = 0; at !== null && step <= nodes.size; step++) {
    if (at === to) return true;
    at = nodes.get(at)?.run.parent ?? null;
  }
  return false;
}

/** A run's state in words, with how it stopped. */
export function stateText(run: Pick<TreeRun, 'state' | 'stopped'>): string {
  const state =
    run.state === 'running'
      ? 'running'
      : run.state === 'ended'
        ? 'ended'
        : 'interrupted: its process died';
  return run.stopped === null ? state : `${state} (${visible(run.stopped)})`;
}

/** A JSON value in words: a string as it is, anything else as JSON. */
function said(value: unknown): string {
  return visible(typeof value === 'string' ? value : JSON.stringify(value));
}

/** Limits in words, such as `max_turns 12, tokens 40000`; empty for none. */
export function limitsText(limits: Record<string, unknown> | null | undefined): string {
  if (limits === null || limits === undefined) return '';
  return Object.entries(limits)
    .map(([name, value]) => `${visible(name)} ${said(value)}`)
    .join(', ');
}

/** A delegation's brief, as the platform wrote it and the parent added to it. */
export interface BriefView {
  /** The platform's task. */
  task: string;
  /** The entry and revision a check names, when it names them. */
  entry: { handle: string; revision: number | null } | null;
  /** The inputs the platform listed, such as the entry's evidence. */
  inputs: string[];
  /** The parent's note: what it added, which is not evidence (LG-17). */
  note: string;
  /** The model chosen: its alias, and the alias asked for when a fallback replaced it. */
  model: string;
  /** The limits granted. */
  limits: string;
  /** A task's context, as its parent wrote it (PD-11). */
  context: string;
  /** The plan track a task works on, and its plan version (PD-9), in words. */
  track: string;
  /** The issue a task works on, by its handle (LG-F8). */
  issue: string;
  /** Whether a task may delegate tasks of its own (PD-15). */
  delegates: boolean;
}

const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

export function briefView(delegation: Pick<Delegation, 'brief'>): BriefView {
  const brief = record(delegation.brief);
  const check = record(brief.check);
  const handle = typeof check.handle === 'string' ? check.handle : null;
  const revision = Number.isSafeInteger(check.revision) ? (check.revision as number) : null;
  const model = record(brief.model);
  const alias = typeof model.alias === 'string' ? visible(model.alias) : '';
  const asked =
    model.fallback === true && typeof model.asked === 'string'
      ? `, the project's fallback for ${visible(model.asked)}`
      : '';
  const track = record(brief.track);
  const plan = record(track.plan);
  const issue = record(brief.issue);
  return {
    task: typeof brief.task === 'string' ? visible(brief.task) : '',
    entry: handle === null ? null : { handle, revision },
    inputs: Array.isArray(brief.inputs) ? brief.inputs.map(said) : [],
    note: typeof brief.note === 'string' ? visible(brief.note) : '',
    model: alias ? `${alias}${asked}` : 'the delegated role’s',
    limits: limitsText(record(brief.limits)),
    context: typeof brief.context === 'string' ? visible(brief.context) : '',
    track:
      typeof track.track_id === 'string'
        ? `${visible(track.track_id)}${
            typeof plan.plan_id === 'string' && Number.isSafeInteger(plan.version)
              ? ` of plan ${visible(plan.plan_id)} version ${plan.version as number}`
              : ''
          }`
        : '',
    issue: typeof issue.handle === 'string' ? visible(issue.handle) : '',
    delegates: brief.delegate === true,
  };
}

/** How a delegation ended, in words: the platform's observation, then the run's stop. */
export function endedText(delegation: Pick<Delegation, 'ended' | 'stopped'>): string {
  if (delegation.ended === null) return 'Running: no outcome yet.';
  const ended = delegation.ended.replaceAll('_', ' ');
  const stopped = delegation.stopped === null ? '' : ` (${visible(delegation.stopped)})`;
  return `It ${ended === 'returned' ? 'returned' : `ended: ${ended}`}${stopped}.`;
}

/** A check's result as the delegated run returned it: its claims, each as text. */
export interface ResultView {
  verdict: string;
  status: string;
  summary: string;
  examined: string[];
  notExamined: string[];
  refutation: string;
  obstacle: string;
  deviations: string[];
  openQuestions: string[];
  /** A task's dropped lines of work, each with what ran out (LG-F8). */
  dismissals: string[];
  /** A task's claimed obstructions, each with its evidence (LG-F8). */
  obstructions: string[];
  /** The paths a task declared as its results (RA-38). */
  resultFiles: string[];
}

const texts = (value: unknown): string[] => (Array.isArray(value) ? value.map(said) : []);
const text = (value: unknown): string => (value === null || value === undefined ? '' : said(value));

/** Each object of a list as its two fields in words, `first: second`. */
const pairs = (value: unknown, first: string, second: string): string[] =>
  Array.isArray(value)
    ? value.map((item) => {
        const fields = record(item);
        return `${text(fields[first])}: ${text(fields[second])}`;
      })
    : [];

/** The result's fields; null when the run returned none that validated. */
export function resultView(delegation: Pick<Delegation, 'result'>): ResultView | null {
  if (delegation.result === null) return null;
  const result = record(delegation.result);
  const coverage = record(result.coverage);
  return {
    verdict: text(result.verdict),
    status: text(result.status),
    summary: text(result.summary),
    examined: texts(coverage.examined),
    notExamined: texts(coverage.not_examined),
    refutation: text(result.refutation),
    obstacle: text(result.obstacle),
    deviations: texts(result.deviations),
    openQuestions: texts(result.open_questions),
    dismissals: pairs(result.dismissals, 'what', 'limit'),
    obstructions: pairs(result.obstructions, 'claim', 'evidence'),
    resultFiles: texts(result.result_files),
  };
}

/** What the platform observed of a check's reading, in words; empty when it says nothing,
 * as for a task, which has no entry to read. */
export function readingText(
  delegation: Pick<Delegation, 'kind' | 'read_entry' | 'read_proofs'>,
): string {
  if (delegation.kind === 'task') return '';
  const parts: string[] = [];
  if (delegation.read_entry !== null) {
    parts.push(
      delegation.read_entry
        ? 'read the entry at its revision'
        : 'did not read the entry at its revision',
    );
  }
  if (delegation.read_proofs !== null) {
    parts.push(
      delegation.read_proofs ? "read each proof's target" : "did not read each proof's target",
    );
  }
  return parts.length ? `The platform observed that it ${parts.join(' and ')}.` : '';
}

/** Each delegation by its ID. */
export function delegationsById(delegations: readonly Delegation[]): Map<string, Delegation> {
  return new Map(delegations.map((delegation) => [delegation.delegation_id, delegation]));
}

/** One file a delegated run's calls recorded, as the tree shows it. */
export interface FileView {
  /** Its path, each character a person may not see as its code point. */
  path: string;
  /** What the revision is: kept, a tombstone, or one whose bytes are not kept. */
  what: string;
}

/** The files a delegated run's calls recorded (RA-36), and what they could not record. */
export interface FilesView {
  total: number;
  files: FileView[];
  /** What the calls left out or cut short, in words, by reason. */
  gaps: string[];
}

const GAP_WORDS: Record<string, (count: number) => string> = {
  incomplete: (n) => `${n} ${n === 1 ? 'list' : 'lists'} the worker cut short`,
  failed: (n) => `${n} ${n === 1 ? 'recording' : 'recordings'} that stopped part way`,
};

/** A delegation's recorded files; null when its calls recorded nothing and left nothing out. */
export function filesView(delegation: Pick<Delegation, 'files'>): FilesView | null {
  if (delegation.files === null) return null;
  const files = record(delegation.files);
  const listed = Array.isArray(files.files) ? files.files.map(record) : [];
  const gaps = Object.entries(record(files.gaps))
    .filter(([, count]) => Number.isSafeInteger(count) && (count as number) > 0)
    .map(([reason, count]) =>
      reason in GAP_WORDS
        ? GAP_WORDS[reason]!(count as number)
        : `${count as number} left out: ${visible(reason)}`,
    );
  return {
    total: Number.isSafeInteger(files.total) ? (files.total as number) : listed.length,
    files: listed.map((file) => {
      const revision = Number.isSafeInteger(file.revision) ? ` r${file.revision as number}` : '';
      const sha =
        typeof file.sha256 === 'string' ? `, sha256 ${visible(file.sha256.slice(0, 12))}…` : '';
      const size = Number.isSafeInteger(file.size) ? `, ${sizeText(file.size as number)}` : '';
      const what =
        file.deleted === true
          ? `deleted${revision}: a tombstone`
          : `${file.reproduced === true ? 'the same bytes again' : 'written'}${revision}${size}${sha}` +
            (file.content_retained === false ? '; bytes not kept' : '');
      return {
        path: visible(typeof file.path === 'string' ? file.path : String(file.path)),
        what: `${visible(typeof file.artifact_id === 'string' ? file.artifact_id : '?')}, ${what}`,
      };
    }),
    gaps,
  };
}

/** An agent's name in words: the research agent, a check by the entry it checks, or a
 * subagent by the first line of its task. */
export function agentName(
  run: Pick<TreeRun, 'role'>,
  delegation: Pick<Delegation, 'brief' | 'kind'> | null,
): string {
  if (run.role === 'main') return 'Research agent';
  const brief = delegation ? briefView(delegation) : null;
  if (delegation?.kind === 'check')
    return brief?.entry ? `Check of ${brief.entry.handle}` : 'Independent check';
  if (!brief?.task) return 'Subagent';
  const line =
    brief.task
      .split('\n')
      .find((part) => part.trim())
      ?.trim() ?? '';
  return line.length > 70 ? `${line.slice(0, 69)}…` : line;
}
