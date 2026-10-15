// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ArtifactRow from '../views/ArtifactRow.svelte';
import type { ArtifactEntry } from './api-types';
import {
  artifactQuery,
  byRevision,
  type FileSettings,
  keptText,
  NO_FILTER,
  patternProblem,
  patternsOf,
  patternsText,
  saveSettings,
  type SettingsForm,
  settingsChange,
  typeText,
  whoText,
} from './artifactFiles';
import { ApiError } from './http';
import { pdfHref } from './router';
import { idle } from './submit';

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
const pdf: ArtifactEntry = {
  ...entry,
  path: 'sessions/s1/paper.pdf',
  media_type: 'application/pdf',
};

afterEach(() => vi.unstubAllGlobals());

describe('the artifact list', () => {
  it('asks for a group, its filters and the next page', () => {
    expect(artifactQuery('results', NO_FILTER, null)).toBe('group=results');
    expect(artifactQuery('files', { session: ' s1 ', run: 'r1', contains: 'a b' }, 42)).toBe(
      'group=files&session=s1&run=r1&contains=a+b&before=42',
    );
    expect(byRevision(NO_FILTER)).toBe(false);
    expect(byRevision({ ...NO_FILTER, run: 'r1' })).toBe(true);
    expect(byRevision({ ...NO_FILTER, contains: 'x' })).toBe(false);
  });

  it('says who made the latest revision, and never guesses a kind', () => {
    expect(whoText(entry)).toBe('delegated run run-1, call call3');
    expect(whoText({ ...entry, kind: 'agent_publication', role: 'main' })).toBe(
      'published by the main run run-1',
    );
    expect(whoText({ ...entry, kind: 'declared_result' })).toBe(
      'declared by the delegated run run-1',
    );
    expect(whoText({ ...entry, kind: 'later_kind' })).toBe('by the delegated run run-1');
  });

  it('says what was not kept, and that a tombstone has no type', () => {
    expect(keptText(entry)).toBe('');
    expect(keptText({ ...entry, deleted: true, content_retained: false })).toBe('deleted');
    expect(keptText({ ...entry, content_retained: false, content_note: 'too_large' })).toBe(
      'bytes not kept: too large',
    );
    expect(keptText({ ...entry, content_retained: false, content_note: 'quota' })).toMatch(
      /budget was full/,
    );
    expect(typeText({ ...entry, deleted: true })).toBe('deleted');
    expect(typeText(entry)).toBe('text/markdown');
  });
});

function row(item: ArtifactEntry, viewer = true): string {
  return render(ArtifactRow, { props: { projectId: 'p1', entry: item, viewer } }).body;
}

describe('an artifact row', () => {
  it('shows a hostile path as text, isolated from the text around it', () => {
    const html = row({ ...entry, path: 'javascript:x"><b>bold</b>' });
    expect(html).not.toContain('<b>');
    expect(html).toContain('<bdi>javascript:x">&lt;b>bold&lt;/b></bdi>');
    expect(html).not.toMatch(/href="javascript/);
    const reversed = row({ ...entry, path: 'notes/‮fdp.exe' });
    expect(reversed).toContain('<bdi>notes/‮fdp.exe</bdi>');
  });

  it('links a kept PDF to the viewer, exactly, and nothing else', () => {
    const html = row(pdf);
    const href = pdfHref('p1', pdf.artifact_id, pdf.revision, pdf.sha256);
    expect(html).toContain(`href="${href}"`);
    expect(row({ ...pdf, deleted: true, content_retained: false })).not.toContain('<a');
    expect(
      row({ ...pdf, content_retained: false, content_note: 'too_large', size: 2 * 1024 ** 3 }),
    ).not.toContain('<a');
    expect(row(pdf, false)).not.toContain('<a');
  });

  it('shows a tombstone as deleted, without a size', () => {
    const html = row({ ...entry, deleted: true, content_retained: false });
    expect(html).toContain('<code>deleted</code>');
    expect(html).toMatch(/<tr class="[^"]*\bdeleted\b/);
  });
});

describe('the views', () => {
  it('render no text as HTML', () => {
    const views = import.meta.glob(
      [
        '../views/ArtifactList.svelte',
        '../views/ArtifactTable.svelte',
        '../views/ArtifactRow.svelte',
        '../views/ProjectFiles.svelte',
      ],
      { query: '?raw', import: 'default', eager: true },
    );
    expect(Object.keys(views)).toHaveLength(4);
    expect(Object.values(views).map(String).join('\n')).not.toContain('{@html');
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

  it('names the first line the server would refuse', () => {
    expect(patternProblem('.cache/\n*.pyc\n')).toBeNull();
    expect(patternProblem('.cache/\n\n!keep.txt')).toBe('line 3: negation (!) is not supported');
    expect(patternProblem('a//b')).toBe('line 1: an empty, . or .. segment');
    expect(patternProblem(`x\n${'y'.repeat(257)}`)).toMatch(/^line 2: /);
  });

  const current: FileSettings = { project: 'p1', revision: 4, recording: 'all', ignore: ['a/'] };

  function form(): SettingsForm {
    return { expected: 3, head: 3, recording: 'results', text: 'tmp/', status: '' };
  }

  it('keeps the edits and shows the current revision after a conflict', async () => {
    const edited = form();
    const saving = idle();
    const send = vi.fn().mockRejectedValue(new ApiError(409, 'conflict', 'changed meanwhile'));
    const done = await saveSettings(edited, saving, 'p1', send, async () => current);
    expect(done).toBe(false);
    expect((saving.error as ApiError).status).toBe(409);
    expect(edited).toEqual({
      expected: 3,
      head: 4,
      recording: 'results',
      text: 'tmp/',
      status: '',
    });
    expect(send.mock.calls[0]?.[0]).toMatchObject({ expected: 3, ignore: ['tmp/'] });
  });

  it('keeps the edits after a refused pattern, whose line the server names', async () => {
    const edited = { ...form(), text: 'tmp/\n[z-a]' };
    const saving = idle();
    const refused = new ApiError(400, 'invalid_request', "line 2: invalid ignore pattern '[z-a]'");
    const send = vi.fn().mockRejectedValue(refused);
    expect(await saveSettings(edited, saving, 'p1', send, async () => current)).toBe(false);
    expect((saving.error as Error).message).toMatch(/^line 2:/);
    expect(edited.text).toBe('tmp/\n[z-a]');
  });

  it('refuses before sending what the browser can see is wrong', async () => {
    const edited = { ...form(), text: '!keep' };
    const saving = idle();
    const send = vi.fn();
    expect(await saveSettings(edited, saving, 'p1', send, async () => current)).toBe(false);
    expect(send).not.toHaveBeenCalled();
    expect((saving.error as Error).message).toMatch(/^line 1: negation/);
  });

  it('takes the saved settings', async () => {
    const edited = form();
    const saved: FileSettings = { ...current, revision: 4, recording: 'results', ignore: ['tmp/'] };
    const send = vi.fn().mockResolvedValue(saved);
    expect(await saveSettings(edited, idle(), 'p1', send, async () => current)).toBe(true);
    expect(edited).toMatchObject({ expected: 4, head: 4, text: 'tmp/' });
    expect(edited.status).toMatch(/Saved revision 4/);
  });
});
