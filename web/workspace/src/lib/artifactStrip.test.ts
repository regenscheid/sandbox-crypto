// SPDX-License-Identifier: MIT
// The strip of files under a work group (B10, item 6): the glosses' paths matched to the
// run's artifact list by call and path, each file's kind, and when the list is read.
import { describe, expect, it, vi } from 'vitest';
import type { ArtifactEntry } from './api-types';
import {
  keptChoice,
  matchEntry,
  RunArtifacts,
  stripChips,
  stripCounts,
  stripKind,
  type RunListing,
} from './artifactStrip';
import type { Gloss } from './chat';

const entry = (more: Partial<ArtifactEntry> = {}): ArtifactEntry => ({
  artifact_id: 'art-1',
  revision: 1,
  path: 'results/table.csv',
  sha256: 'ab'.repeat(32),
  size: 2048,
  media_type: 'text/csv',
  created_at: '2026-10-25T10:00:00Z',
  result: false,
  deleted: false,
  content_retained: true,
  content_note: null,
  kind: 'execution_record',
  session_id: 's1',
  run_id: 'run-1',
  role: 'main',
  tool_call: 'call-1',
  ...more,
});

const gloss = (files: string[], more_files = 0): Gloss => ({
  outcome: 'ok',
  exit_code: 0,
  lines: null,
  bytes: null,
  files,
  more_files,
  error: null,
});

describe('stripKind', () => {
  it('tells tables, figures, PDFs, text and other files apart', () => {
    expect(stripKind('a/b.csv', null)).toBe('table');
    expect(stripKind('b.tsv', null)).toBe('table');
    expect(stripKind('data', { media_type: 'text/csv' })).toBe('table');
    expect(stripKind('plot.png', null)).toBe('figure');
    expect(stripKind('plot', { media_type: 'image/svg+xml' })).toBe('figure');
    expect(stripKind('paper.pdf', null)).toBe('pdf');
    expect(stripKind('notes.md', null)).toBe('text');
    expect(stripKind('out', { media_type: 'text/plain' })).toBe('text');
    expect(stripKind('blob.bin', { media_type: 'application/octet-stream' })).toBe('other');
  });
});

describe('matchEntry', () => {
  it('takes the entry the same call recorded at the path', () => {
    const entries = [
      entry({ artifact_id: 'art-2', tool_call: 'call-2', revision: 2 }),
      entry({ artifact_id: 'art-1', tool_call: 'call-1', revision: 1 }),
    ];
    expect(matchEntry('results/table.csv', 'call-1', 1, entries)?.artifact_id).toBe('art-1');
  });
  it("else the path's latest entry, else a call's one publication of its one path", () => {
    const later = entry({ tool_call: 'call-9', revision: 3 });
    expect(matchEntry('results/table.csv', 'call-1', 1, [later])).toBe(later);
    const published = entry({ path: 'figures/plot.png', tool_call: 'call-5' });
    expect(matchEntry('work/plot.png', 'call-5', 1, [published])).toBe(published);
    expect(matchEntry('work/plot.png', 'call-5', 2, [published])).toBeNull();
    expect(matchEntry('missing.txt', 'call-1', 1, [entry({ tool_call: 'call-2' })])).toBeNull();
  });
});

describe('stripChips', () => {
  it('makes one chip per path, matched or "not yet listed", with the files left out', () => {
    const calls = [
      { id: 'call-1', gloss: gloss(['results/table.csv', 'plot.png']) },
      { id: 'call-2', gloss: gloss(['results/table.csv'], 4) },
      { id: 'call-3' },
    ];
    const listed = [
      entry({ tool_call: 'call-2', revision: 2 }),
      entry({
        artifact_id: 'art-3',
        path: 'plot.png',
        media_type: 'image/png',
        tool_call: 'call-1',
      }),
    ];
    const { chips, more } = stripChips(calls, listed, true);
    expect(more).toBe(4);
    expect(chips.map((chip) => [chip.path, chip.call, chip.kind])).toEqual([
      ['plot.png', 'call-1', 'figure'],
      ['results/table.csv', 'call-2', 'table'],
    ]);
    expect(chips[1]).toMatchObject({ name: 'table.csv', folder: 'results', size: '2.0 KiB' });
    expect(chips[1]!.kindText).toBe('Table · CSV table');
    expect(chips[0]!.thumbnail).toBe(true);
    expect(stripChips(calls, listed, false).chips[0]!.thumbnail).toBe(false);
    const unlisted = stripChips(calls, null, true).chips;
    expect(unlisted.every((chip) => chip.entry === null && chip.size === null)).toBe(true);
    expect(unlisted[0]!.kindText).toBe('Figure');
  });
  it('shows hidden characters in a path as code points', () => {
    const { chips } = stripChips([{ id: 'c', gloss: gloss(['a‮b.txt']) }], null, false);
    expect(chips[0]!.name).toBe('a⟦U+202E⟧b.txt');
  });
});

describe('RunArtifacts', () => {
  it('reads a run once, again for new paths while it runs, and once after it ends', async () => {
    const state: Record<string, RunListing> = {};
    const read = vi.fn(async () => ({ entries: [entry()], viewer: true }));
    const runs = new RunArtifacts(state, read);
    await runs.need('run-1', 0, 0, true);
    expect(read).not.toHaveBeenCalled();
    await runs.need('run-1', 1, 1, true);
    expect(read).toHaveBeenCalledTimes(1);
    expect(state['run-1']).toMatchObject({ viewer: true, loading: false, paths: 1 });
    await runs.need('run-1', 1, 1, true);
    expect(read).toHaveBeenCalledTimes(1);
    await runs.need('run-1', 2, 1, true);
    expect(read).toHaveBeenCalledTimes(2);
    await runs.need('run-1', 2, 1, false);
    expect(read).toHaveBeenCalledTimes(3);
    await runs.need('run-1', 2, 1, false);
    expect(read).toHaveBeenCalledTimes(3);
  });
  it('keeps a failed read, without reading again on its own', async () => {
    const state: Record<string, RunListing> = {};
    const read = vi.fn(async () => {
      throw new Error('offline');
    });
    const runs = new RunArtifacts(state, read);
    await runs.need('run-1', 1, 1, false);
    await runs.need('run-1', 1, 1, false);
    expect(read).toHaveBeenCalledTimes(1);
    expect(state['run-1']!.error).toBe('offline');
  });
  it('counts the paths and those the list lacks', () => {
    const calls = [{ id: 'call-1', gloss: gloss(['results/table.csv', 'x.txt']) }];
    expect(stripCounts(calls, null)).toEqual({ paths: 2, unmatched: 2 });
    expect(stripCounts(calls, [entry()])).toEqual({ paths: 2, unmatched: 1 });
  });
});

describe('keptChoice', () => {
  it('reads the panel choice back from the browser state, or none', () => {
    expect(keptChoice({ artifact: 'art-1', revision: '3' })).toEqual({
      artifact: 'art-1',
      revision: 3,
      entry: null,
    });
    expect(keptChoice({ artifact: 'art-1', revision: 'x' })).toBeNull();
    expect(keptChoice({})).toBeNull();
  });
});
