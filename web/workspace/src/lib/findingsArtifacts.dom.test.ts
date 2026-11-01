// SPDX-License-Identifier: MIT
// The reviewer's finding as a row of the conversation, and the strip of files under a work
// group (B10, items 4 and 6), as the browser shows them.
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ArtifactPanel from '../views/ArtifactPanel.svelte';
import ArtifactStrip from '../views/ArtifactStrip.svelte';
import ChatRun from '../views/ChatRun.svelte';
import FindingRow from '../views/FindingRow.svelte';
import type { ArtifactEntry, RunEvent } from './api-types';
import { RunArtifacts, type RunListing } from './artifactStrip';
import type { StepCall } from './conversation';
import { emptyFindings, SessionFindings } from './findingRows';
import { reactiveProps } from './reactiveProps.svelte';
import type { Finding } from './reviews';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
});

function place<Props extends Record<string, unknown>>(component: Component<Props>, props: Props) {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(component, { target, props }));
  flushSync();
  return target;
}

const finding = (more: Partial<Finding> = {}): Finding => ({
  finding_id: 'finding-1',
  review_id: 'review-1',
  severity: 'fail',
  category: 'unsupported_claim',
  statement: 'Claim C3‮ is not supported.',
  remedy: null,
  status: 'open',
  revision: 2,
  reflags: 0,
  anchors: [{ locator: { entry: 'ent-7', handle: 'C3', revision: 4, run: 'run-1' } }],
  ...more,
});

it("shows the reviewer's statement as text, hidden characters as code points", () => {
  const target = place(FindingRow, {
    projectId: 'p1',
    sessionId: 's1',
    notice: { finding: 'finding-1', revision: 2 },
    finding: finding({ statement: 'Claim C3‮ <b>is</b> not supported.' }),
  });
  const statement = target.querySelector('.finding-statement')!;
  expect(statement.textContent).toContain('Claim C3⟦U+202E⟧ <b>is</b> not supported.');
  expect(statement.querySelector('b')).toBeNull();
  expect(target.querySelector('.finding-chip')!.classList.contains('status-open')).toBe(true);
  const ledger = target.querySelector<HTMLAnchorElement>('.finding-anchors a')!;
  expect(ledger.textContent).toBe('ledger entry C3 revision 4');
  expect(ledger.getAttribute('href')).toBe('#/projects/p1/ledger/ent-7/revisions/4');
  expect(target.querySelector<HTMLAnchorElement>('.finding-card-link')!.getAttribute('href')).toBe(
    '#/projects/p1/sessions/s1/findings/finding-1',
  );
});

it('shows a superseded finding with its successor, and one not read yet', () => {
  const superseded = place(FindingRow, {
    projectId: 'p1',
    sessionId: 's1',
    notice: { finding: 'finding-1', revision: 2 },
    finding: finding({ status: 'superseded', superseded_by: 'finding-9', revision: 3 }),
  });
  expect(superseded.querySelector('.finding-chip')!.textContent).toBe('Superseded');
  expect(superseded.textContent).toContain('Replaced by finding-9');
  expect(superseded.textContent).toContain('Offered at revision 2; now at revision 3.');
  const pending = place(FindingRow, {
    projectId: 'p1',
    sessionId: 's1',
    notice: { finding: 'finding-2', revision: 1 },
  });
  expect(pending.textContent).toContain('Reading the finding…');
});

it('updates a row in the conversation when its finding is read again', async () => {
  const state = reactiveProps(emptyFindings());
  const read = vi.fn(async () => finding());
  const findings = new SessionFindings(state, read);
  const event = (seq: number, kind: RunEvent['kind'], data: Record<string, unknown>) =>
    ({ seq, at: '2026-10-25T10:00:00Z', kind, data, pins: [] }) as RunEvent;
  const target = place(ChatRun, {
    run: {
      run: 'run-1',
      state: 'ended',
      events: [event(1, 'notice', { source: 'reviews', ids: ['finding-1-r2'] })],
    },
    projectId: 'p1',
    sessionId: 's1',
    findings,
  });
  expect(target.textContent).toContain('Reading the finding…');
  await findings.refresh(false);
  flushSync();
  expect(target.querySelector('.finding-chip')!.textContent).toBe('Open');
  read.mockImplementation(async () => finding({ status: 'fixed', revision: 3 }));
  await findings.refresh(true);
  flushSync();
  expect(target.querySelector('.finding-chip')!.textContent).toBe('Fixed');
});

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

const call = (id: string, files: string[], more_files = 0): StepCall => ({
  id,
  name: 'shell',
  arguments: '',
  done: true,
  outcome: 'ok',
  result: '',
  gloss: {
    outcome: 'ok',
    exit_code: 0,
    lines: 3,
    bytes: null,
    files,
    more_files,
    error: null,
  },
});

it('shows a listed file as a chip that opens it, and an unlisted one as text', () => {
  const show = vi.fn();
  const listing: RunListing = {
    entries: [entry()],
    viewer: false,
    loading: false,
    error: null,
    paths: 2,
    live: false,
  };
  const target = place(ArtifactStrip, {
    projectId: 'p1',
    sessionId: 's1',
    run: 'run-1',
    calls: [call('call-1', ['results/table.csv', 'notes​.md'], 3)],
    listing,
    show,
  });
  const chip = target.querySelector<HTMLButtonElement>('button.artifact-chip')!;
  expect(chip.dataset['kind']).toBe('table');
  expect(chip.textContent).toContain('table.csv');
  expect(chip.textContent).toContain('Table · CSV table');
  expect(chip.textContent).toContain('2.0 KiB');
  chip.click();
  expect(show).toHaveBeenCalledWith(listing.entries[0]);
  const unlisted = target.querySelector('.artifact-chip.unlisted')!;
  expect(unlisted.textContent).toContain('notes⟦U+200B⟧.md');
  expect(unlisted.textContent).toContain('not yet listed');
  expect(target.querySelector<HTMLAnchorElement>('.artifact-more')!.textContent).toBe(
    '3 more files',
  );
  expect(target.querySelector<HTMLAnchorElement>('.artifact-more')!.getAttribute('href')).toBe(
    '#/projects/p1/files/s1/run-1',
  );
});

it("puts a strip under a work group once the run's list is read", async () => {
  const state = reactiveProps<Record<string, RunListing>>({});
  const read = vi.fn(async () => ({ entries: [entry()], viewer: false }));
  const artifacts = new RunArtifacts(state, read);
  const event = (seq: number, kind: RunEvent['kind'], data: Record<string, unknown>) =>
    ({ seq, at: '2026-10-25T10:00:00Z', kind, data, pins: [] }) as RunEvent;
  const target = place(ChatRun, {
    run: {
      run: 'run-1',
      state: 'ended',
      events: [
        event(1, 'assistant', {
          text: '',
          tool_calls: [{ id: 'call-1', name: 'shell', arguments: '{}' }],
        }),
        event(2, 'tool', {
          call_id: 'call-1',
          name: 'shell',
          outcome: 'ok',
          result: '',
          gloss: call('call-1', ['results/table.csv']).gloss,
        }),
      ],
    },
    projectId: 'p1',
    sessionId: 's1',
    artifacts,
    showArtifact: () => {},
  });
  expect(target.textContent).toContain('not yet listed');
  for (let i = 0; i < 4; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
  expect(read).toHaveBeenCalledTimes(1);
  expect(target.querySelector('button.artifact-chip')!.textContent).toContain('table.csv');
});

async function settle() {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

it("lists the session's artifacts in the panel, and reads a kept choice back", async () => {
  const listed = entry({ artifact_id: 'art-5', path: 'out/figure.png', media_type: 'image/png' });
  const fetch = vi.fn(async (url: string) =>
    url.includes('/revisions')
      ? new Response(JSON.stringify({ revisions: [listed], next_before: null }))
      : new Response(JSON.stringify({ artifacts: [listed], next_before: null, viewer: false })),
  );
  vi.stubGlobal('fetch', fetch);
  try {
    const choose = vi.fn();
    const list = place(ArtifactPanel, { projectId: 'p1', sessionId: 's1', choice: null, choose });
    await settle();
    expect(String(fetch.mock.calls[0]![0])).toContain('/projects/p1/artifacts?session=s1');
    const open = list.querySelector<HTMLButtonElement>('.artifact-latest button')!;
    expect(open.textContent).toBe('figure.png');
    open.click();
    expect(choose).toHaveBeenCalledWith(listed);
    const kept = vi.fn();
    place(ArtifactPanel, {
      projectId: 'p1',
      sessionId: 's1',
      choice: { artifact: 'art-5', revision: 1, entry: null },
      choose: kept,
    });
    await settle();
    expect(kept).toHaveBeenCalledWith(listed);
  } finally {
    vi.unstubAllGlobals();
  }
});
