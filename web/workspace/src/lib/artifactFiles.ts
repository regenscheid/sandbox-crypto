// SPDX-License-Identifier: MIT
/**
 * The artifact list's and the file settings' decisions, apart from their views
 * (ArtifactList.svelte, ArtifactTable.svelte, ArtifactRow.svelte, ProjectFiles.svelte;
 * research artifacts, RA-29, RA-37, RA-39).
 */
import type { ArtifactEntry } from './api-types';
import { guardedWrite } from './mutation';
import { type Submitting, submit } from './submit';

/** Results (published or declared), or every other recorded file. */
export type ArtifactGroup = 'results' | 'files';

export interface ArtifactFilter {
  session: string;
  run: string;
  /** Text the path has. */
  contains: string;
}

export const NO_FILTER: ArtifactFilter = { session: '', run: '', contains: '' };

/** Whether a filter keeps artifacts by a revision of a session or run, which need not be
 * the latest revision the list shows. */
export function byRevision(filter: ArtifactFilter): boolean {
  return filter.session.trim() !== '' || filter.run.trim() !== '';
}

/** The listing's query: a group, the filters given and the page before ``before``. */
export function artifactQuery(
  group: ArtifactGroup,
  filter: ArtifactFilter,
  before: number | null,
): string {
  const query = new URLSearchParams({ group });
  if (filter.session.trim()) query.set('session', filter.session.trim());
  if (filter.run.trim()) query.set('run', filter.run.trim());
  if (filter.contains) query.set('contains', filter.contains);
  if (before !== null) query.set('before', String(before));
  return query.toString();
}

/** Who made the entry's latest revision, and how. */
export function whoText(entry: ArtifactEntry): string {
  const run = `${entry.role === 'delegated' ? 'delegated' : 'main'} run ${entry.run_id}`;
  switch (entry.kind) {
    case 'execution_record':
      return `${run}, call ${entry.tool_call}`;
    case 'declared_result':
      return `declared by the ${run}`;
    case 'agent_publication':
      return `published by the ${run}`;
    default:
      return `by the ${run}`;
  }
}

/** What the latest revision kept: nothing to say, its bytes elsewhere, or a deletion. */
export function keptText(entry: ArtifactEntry): string {
  if (entry.deleted) return 'deleted';
  if (entry.content_retained) return '';
  return entry.content_note === 'quota'
    ? 'bytes not kept: the storage budget was full'
    : 'bytes not kept: too large';
}

/** The type column: a tombstone has none. */
export function typeText(entry: ArtifactEntry): string {
  return entry.deleted ? 'deleted' : entry.media_type;
}

export interface FileSettings {
  project: string;
  revision: number;
  recording: 'all' | 'results';
  ignore: string[];
  defaults?: string[];
  replayed?: boolean;
}

/** The patterns as the editor shows them: one a line. */
export function patternsText(patterns: readonly string[]): string {
  return patterns.join('\n');
}

/** The patterns a text names: one a line, blank lines left out. */
export function patternsOf(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '');
}

/**
 * The first line the server would refuse for a reason the browser can see, named by its
 * line in the editor; the server checks the rest (a class such as `[z-a]`, say) and names
 * the line too.
 */
export function patternProblem(text: string): string | null {
  const lines = text.split('\n');
  for (const [index, raw] of lines.entries()) {
    const line = raw.trim();
    if (!line) continue;
    const at = `line ${index + 1}`;
    if (line.startsWith('!')) return `${at}: negation (!) is not supported`;
    if (line.length > 256) return `${at}: a pattern has at most 256 characters`;
    if ([...line].some((c) => c < ' ' || c === '\u007f')) return `${at}: control characters`;
    const parts = line.replace(/\/+$/, '').replace(/^\//, '').split('/');
    if (parts.some((part) => part === '' || part === '.' || part === '..')) {
      return `${at}: an empty, . or .. segment`;
    }
  }
  return null;
}

/** The change a save sends; the server checks the patterns. */
export function settingsChange(
  expected: number,
  recording: 'all' | 'results',
  text: string,
): { expected: number; recording: 'all' | 'results'; ignore: string[] } {
  return { expected, recording, ignore: patternsOf(text) };
}

/** The settings form: what the user edits, the revision it is based on, and the
 * current one. */
export interface SettingsForm {
  expected: number;
  head: number;
  recording: 'all' | 'results';
  text: string;
  status: string;
}

/**
 * Save the form: with the revision it read and a retried operation ID. On success the
 * form takes the saved settings; on a failure (a conflict, a refused pattern) it keeps
 * the user's edits, reads the current revision, and `saving.error` says why.
 */
export async function saveSettings(
  form: SettingsForm,
  saving: Submitting,
  scope: string,
  send: (change: Record<string, unknown>) => Promise<FileSettings>,
  read: () => Promise<FileSettings>,
): Promise<boolean> {
  form.status = '';
  const problem = patternProblem(form.text);
  if (problem !== null) {
    saving.error = new Error(problem);
    return false;
  }
  return submit(
    saving,
    async () => {
      const change = settingsChange(form.expected, form.recording, form.text);
      const saved = await guardedWrite('files', scope, change, send);
      form.expected = form.head = saved.revision;
      form.recording = saved.recording;
      form.text = patternsText(saved.ignore);
      form.status = `Saved revision ${saved.revision}. Calls that end from now on follow it.`;
    },
    async () => {
      form.head = (await read()).revision; // beside the edits the user keeps
    },
  );
}
