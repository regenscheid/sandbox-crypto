// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { ArtifactEntry } from './api-types';
import {
  clampWidth,
  firstDirection,
  kindText,
  sortEntries,
  splitPath,
  shortTime,
  textual,
} from './artifactGrid';

const entry = (path: string, fields: Partial<ArtifactEntry> = {}): ArtifactEntry => ({
  artifact_id: `art-${path}`,
  revision: 1,
  path,
  sha256: 'a'.repeat(64),
  size: 10,
  media_type: 'text/plain',
  created_at: '2026-10-21T18:00:00Z',
  result: false,
  deleted: false,
  content_retained: true,
  content_note: null,
  kind: 'execution_record',
  session_id: 'main',
  run_id: 'r1',
  role: 'main',
  tool_call: 'c1',
  ...fields,
});

describe('the artifacts table', () => {
  const rows = [
    entry('sessions/main/b.sage', { revision: 2, size: 300, created_at: '2026-10-21T18:56:00Z' }),
    entry('sessions/main/a.md', {
      media_type: 'text/markdown',
      created_at: '2026-10-21T18:59:00Z',
    }),
    entry('shared/c.log', { size: 0, created_at: '2026-10-21T18:55:00Z' }),
  ];
  const names = (sorted: ArtifactEntry[]) => sorted.map((row) => splitPath(row.path).name);

  it('sorts by each column, both ways', () => {
    expect(names(sortEntries(rows, 'name', 'asc'))).toEqual(['a.md', 'b.sage', 'c.log']);
    expect(names(sortEntries(rows, 'name', 'desc'))).toEqual(['c.log', 'b.sage', 'a.md']);
    expect(names(sortEntries(rows, 'when', 'desc'))).toEqual(['a.md', 'b.sage', 'c.log']);
    expect(names(sortEntries(rows, 'size', 'desc'))).toEqual(['b.sage', 'a.md', 'c.log']);
    expect(names(sortEntries(rows, 'revision', 'desc'))[0]).toBe('b.sage');
    expect(names(sortEntries(rows, 'kind', 'asc'))).toEqual(['c.log', 'a.md', 'b.sage']);
  });

  it('starts names ascending and numbers and times descending', () => {
    expect(firstDirection('name')).toBe('asc');
    expect(firstDirection('when')).toBe('desc');
    expect(firstDirection('size')).toBe('desc');
  });

  it('keeps a column between its bounds', () => {
    expect(clampWidth(10)).toBe(72);
    expect(clampWidth(5000)).toBe(720);
    expect(clampWidth(140.6)).toBe(141);
  });

  it('names a file by its extension, else its media type', () => {
    expect(kindText(entry('x.sage'))).toBe('Sage script');
    expect(kindText(entry('x', { media_type: 'image/png' }))).toBe('PNG image');
    expect(kindText(entry('x.bin', { media_type: 'application/octet-stream' }))).toBe(
      'application/octet-stream',
    );
    expect(kindText(entry('x.md', { deleted: true }))).toBe('Deleted');
    expect(splitPath('top.txt')).toEqual({ name: 'top.txt', folder: '' });
  });

  it('reads only text as text', () => {
    expect(textual(entry('x.log'))).toBe(true);
    expect(textual(entry('x', { media_type: 'application/json' }))).toBe(true);
    expect(textual(entry('x.png', { media_type: 'image/png' }))).toBe(false);
    expect(textual(entry('x.tar.gz', { media_type: 'application/gzip' }))).toBe(false);
  });

  it('shows the clock for today, and the day before it otherwise', () => {
    const now = new Date('2026-10-21T20:00:00');
    expect(shortTime('2026-10-21T18:59:00', now)).not.toContain('Oct');
    expect(shortTime('2026-10-20T18:59:00', now)).toContain('Oct 20');
    expect(shortTime('2025-10-20T18:59:00', now)).toContain('2025');
  });
});
