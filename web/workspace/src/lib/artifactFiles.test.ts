// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { ArtifactEntry } from './api-types';
import {
  artifactQuery,
  keptText,
  patternsOf,
  patternsText,
  settingsChange,
  whoText,
} from './artifactFiles';

const entry: ArtifactEntry = {
  artifact_id: 'artifact-1',
  revision: 2,
  path: 'sessions/s1/notes/summary.md',
  sha256: 'a'.repeat(64),
  size: 6,
  media_type: 'text/markdown',
  created_at: '2026-10-14T12:00:00Z',
  result: false,
  deleted: false,
  content_retained: true,
  content_note: null,
  kind: 'execution_record',
  session_id: 's1',
  run_id: 'run-1',
  role: 'delegated',
  tool_call: 'call3',
};

describe('the artifact list', () => {
  it('asks for a group, its filters and the next page', () => {
    expect(artifactQuery('results', { session: '', run: '' }, null)).toBe('group=results');
    expect(artifactQuery('files', { session: ' s1 ', run: 'r1' }, 'artifact-9')).toBe(
      'group=files&session=s1&run=r1&after=artifact-9',
    );
  });

  it('says who made the latest revision', () => {
    expect(whoText(entry)).toBe('delegated run run-1, call call3');
    expect(whoText({ ...entry, kind: 'agent_publication', role: 'main' })).toBe(
      'published by the main run run-1',
    );
    expect(whoText({ ...entry, kind: 'declared_result' })).toBe(
      'declared by the delegated run run-1',
    );
  });

  it('says what was not kept', () => {
    expect(keptText(entry)).toBe('');
    expect(keptText({ ...entry, deleted: true, content_retained: false })).toBe('deleted');
    expect(keptText({ ...entry, content_retained: false, content_note: 'too_large' })).toBe(
      'bytes not kept: too large',
    );
    expect(keptText({ ...entry, content_retained: false, content_note: 'quota' })).toMatch(
      /budget was full/,
    );
  });
});

describe('the file settings', () => {
  it('edits the patterns one a line', () => {
    expect(patternsText(['.cache/', '*.pyc'])).toBe('.cache/\n*.pyc');
    expect(patternsOf(' .cache/ \n\n*.pyc\n')).toEqual(['.cache/', '*.pyc']);
    expect(settingsChange(3, 'results', 'tmp/\n')).toEqual({
      expected: 3,
      recording: 'results',
      ignore: ['tmp/'],
    });
  });
});
