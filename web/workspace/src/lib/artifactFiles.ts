// SPDX-License-Identifier: MIT
/**
 * The artifact list's and the file settings' decisions, apart from their views
 * (ArtifactList.svelte, ArtifactTable.svelte, ProjectFiles.svelte; research artifacts,
 * RA-29, RA-37, RA-39).
 */
import type { ArtifactEntry } from './api-types';

/** Results (published or declared), or every other recorded file. */
export type ArtifactGroup = 'results' | 'files';

export interface ArtifactFilter {
  session: string;
  run: string;
}

/** The listing's query: a group, the filters given and the page after ``after``. */
export function artifactQuery(
  group: ArtifactGroup,
  filter: ArtifactFilter,
  after: string | null,
): string {
  const query = new URLSearchParams({ group });
  if (filter.session.trim()) query.set('session', filter.session.trim());
  if (filter.run.trim()) query.set('run', filter.run.trim());
  if (after !== null) query.set('after', after);
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
    default:
      return `published by the ${run}`;
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

/** The change a save sends; the server checks the patterns. */
export function settingsChange(
  expected: number,
  recording: 'all' | 'results',
  text: string,
): { expected: number; recording: 'all' | 'results'; ignore: string[] } {
  return { expected, recording, ignore: patternsOf(text) };
}
