// SPDX-License-Identifier: MIT
/**
 * The files a work group's calls recorded or published, as a strip of chips under it
 * (B10, item 6). The paths come from each `tool` event's gloss (B10, item 1), which the
 * server computes from the tool record; each is matched to its artifact through the
 * project's artifact list filtered to the session and the run, by the call that recorded
 * it (`tool_call`) and its path. A path the list does not have yet shows as text, "not
 * yet listed". Paths are agents' text and show with every hidden character a code point.
 */
import { ARTIFACT_LISTING_MAX_BYTES } from './api';
import type { ArtifactEntry, ArtifactListing } from './api-types';
import { kindText, splitPath, textual } from './artifactGrid';
import type { StepCall } from './conversation';
import { shownAsImage } from './figures';
import { get } from './http';
import { sizeText } from './pdfAnchors';
import { visible } from './text';

/** What a file is, as the strip tells it. */
export type StripKind = 'table' | 'figure' | 'pdf' | 'text' | 'other';

const TABLE_EXTENSIONS = new Set(['csv', 'tsv', 'parquet', 'xlsx', 'xls', 'feather', 'arrow']);
const TABLE_TYPES = new Set([
  'text/csv',
  'text/tab-separated-values',
  'application/vnd.apache.parquet',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.ms-excel',
]);
const FIGURE_EXTENSIONS = new Set([
  'png',
  'jpg',
  'jpeg',
  'gif',
  'svg',
  'webp',
  'bmp',
  'tif',
  'tiff',
]);

const extensionOf = (path: string): string =>
  /\.([A-Za-z0-9]+)$/.exec(path)?.[1]?.toLowerCase() ?? '';

/** A file's kind: from the entry's media type when listed, else from its extension. */
export function stripKind(
  path: string,
  entry: Pick<ArtifactEntry, 'media_type'> | null,
): StripKind {
  const extension = extensionOf(path);
  const media = entry?.media_type ?? '';
  if (TABLE_EXTENSIONS.has(extension) || TABLE_TYPES.has(media)) return 'table';
  if (FIGURE_EXTENSIONS.has(extension) || media.startsWith('image/')) return 'figure';
  if (extension === 'pdf' || media === 'application/pdf') return 'pdf';
  if (textual({ path, media_type: media })) return 'text';
  return 'other';
}

const KIND_WORDS: Record<StripKind, string> = {
  table: 'Table',
  figure: 'Figure',
  pdf: 'PDF',
  text: 'Text',
  other: 'File',
};

/** One chip of the strip. */
export interface StripChip {
  /** The path as the gloss has it, hidden characters as code points. */
  path: string;
  name: string;
  folder: string;
  /** The call that recorded or published it. */
  call: string;
  entry: ArtifactEntry | null;
  kind: StripKind;
  /** The kind in words: "Table · CSV table", or the kind alone for a file not listed. */
  kindText: string;
  size: string | null;
  /** A thumbnail can be shown: an image the preview origin serves. */
  thumbnail: boolean;
}

/**
 * The listed artifact for a path a call's gloss names: the entry the same call recorded at
 * that path; else the latest entry at that path (a later call wrote it again); else, when
 * the call recorded one entry only and named one path only, that entry (a publication
 * whose artifact path differs from the file it was published from).
 */
export function matchEntry(
  path: string,
  call: string,
  named: number,
  entries: readonly ArtifactEntry[],
): ArtifactEntry | null {
  const atPath = entries.filter((entry) => entry.path === path);
  const byCall = atPath.find((entry) => entry.tool_call === call);
  if (byCall) return byCall;
  if (atPath.length) return atPath.reduce((a, b) => (b.revision > a.revision ? b : a));
  const ofCall = entries.filter((entry) => call !== '' && entry.tool_call === call);
  return named === 1 && ofCall.length === 1 ? ofCall[0]! : null;
}

/**
 * The strip of a group's calls: one chip per path, at the last call that named it, in the
 * order the calls ran; and how many more files the glosses counted than they list.
 */
export function stripChips(
  calls: readonly Pick<StepCall, 'id' | 'gloss'>[],
  entries: readonly ArtifactEntry[] | null,
  viewer: boolean,
): { chips: StripChip[]; more: number } {
  const byPath = new Map<string, StripChip>();
  let more = 0;
  for (const call of calls) {
    const gloss = call.gloss;
    if (!gloss) continue;
    more += gloss.more_files;
    for (const path of gloss.files) {
      const entry = entries ? matchEntry(path, call.id, gloss.files.length, entries) : null;
      const kind = stripKind(path, entry);
      const parts = splitPath(path);
      byPath.delete(path);
      byPath.set(path, {
        path: visible(path),
        name: visible(parts.name),
        folder: visible(parts.folder),
        call: call.id,
        entry,
        kind,
        kindText: entry ? `${KIND_WORDS[kind]} · ${visible(kindText(entry))}` : KIND_WORDS[kind],
        size: entry && !entry.deleted ? sizeText(entry.size) : null,
        thumbnail: entry !== null && kind === 'figure' && shownAsImage(entry, viewer),
      });
    }
  }
  return { chips: [...byPath.values()], more };
}

/** The paths the glosses of calls name, and how many of them the list has no entry for. */
export function stripCounts(
  calls: readonly Pick<StepCall, 'id' | 'gloss'>[],
  entries: readonly ArtifactEntry[] | null,
): { paths: number; unmatched: number } {
  let paths = 0,
    unmatched = 0;
  for (const call of calls)
    for (const path of call.gloss?.files ?? []) {
      paths++;
      if (!entries || !matchEntry(path, call.id, call.gloss!.files.length, entries)) unmatched++;
    }
  return { paths, unmatched };
}

/** What the conversation has read of one run's artifacts. */
export interface RunListing {
  entries: ArtifactEntry[];
  /** Whether the preview origin is on, so images may show. */
  viewer: boolean;
  loading: boolean;
  error: string | null;
  /** The paths the run's glosses named when it was last read. */
  paths: number;
  /** Whether it was last read while the run was live. */
  live: boolean;
}

/** The most pages of a run's artifact list one read takes: 100 entries. */
export const RUN_PAGES = 10;

/** A run's artifacts, every group, a page at a time, the most recently changed first. */
export async function readRunArtifacts(
  project: string,
  session: string,
  run: string,
  signal?: AbortSignal,
): Promise<{ entries: ArtifactEntry[]; viewer: boolean }> {
  const entries: ArtifactEntry[] = [];
  let before: number | null = null,
    viewer = false;
  for (let page = 0; page < RUN_PAGES; page++) {
    const query = new URLSearchParams({ session, run });
    if (before !== null) query.set('before', String(before));
    const answer: ArtifactListing = await get<ArtifactListing>(
      `/projects/${encodeURIComponent(project)}/artifacts?${query}`,
      { signal, maxBytes: ARTIFACT_LISTING_MAX_BYTES },
    );
    entries.push(...answer.artifacts);
    viewer = answer.viewer;
    before = answer.next_before;
    if (before === null) break;
  }
  return { entries, viewer };
}

/**
 * The artifact lists of a session's runs, as the conversation's strips need them: a run's
 * list is read when its strip first has paths, again while it runs when its glosses name
 * new paths the list does not have, and once more after it ends if it was read while it
 * ran. The view makes the state with `$state`; a test passes a plain object.
 */
export class RunArtifacts {
  constructor(
    readonly state: Record<string, RunListing>,
    private readonly read: (run: string) => Promise<{ entries: ArtifactEntry[]; viewer: boolean }>,
  ) {}

  /** Whether the run's list should be read now, given what its glosses name. */
  due(run: string, paths: number, unmatched: number, live: boolean): boolean {
    const listing = this.state[run];
    if (paths === 0 || listing?.loading) return false;
    if (!listing) return true;
    if (unmatched === 0) return false;
    // While it runs, only new paths are worth a read; once it ended, one last read.
    return live ? paths > listing.paths : listing.live;
  }

  /** Read the run's list when it is due. */
  async need(run: string, paths: number, unmatched: number, live: boolean): Promise<void> {
    if (!this.due(run, paths, unmatched, live)) return;
    this.state[run] ??= { entries: [], viewer: false, loading: false, error: null, paths: 0, live };
    // Read back, so that a reactive state's own object is the one changed.
    const listing = this.state[run]!;
    listing.loading = true;
    listing.paths = paths;
    listing.live = live;
    try {
      const { entries, viewer } = await this.read(run);
      Object.assign(listing, { entries, viewer, error: null });
    } catch (caught) {
      listing.error = caught instanceof Error ? caught.message : 'The files could not be listed.';
    } finally {
      listing.loading = false;
    }
  }
}

/** The artifact the session's Artifacts panel shows: its ID and revision, and its entry
 * once read (a choice kept across a reload has none until its revisions are read). */
export interface ArtifactChoice {
  artifact: string;
  revision: number;
  entry: ArtifactEntry | null;
}

/** A choice as the session's browser state keeps it, in strings. */
export function keptChoice(kept: Record<string, string>): ArtifactChoice | null {
  const artifact = kept['artifact'] ?? '';
  const revision = Number(kept['revision'] ?? '');
  return artifact && Number.isSafeInteger(revision) && revision > 0
    ? { artifact, revision, entry: null }
    : null;
}
