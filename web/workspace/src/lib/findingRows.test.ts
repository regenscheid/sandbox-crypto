// SPDX-License-Identifier: MIT
// The reviewer's findings as rows of the conversation (B10, item 4; CR-41, CR-46): a
// notice's IDs, the row's model for each status, the re-check that could not decide, the
// ledger link, the successor, and the reads that keep a row current.
import { describe, expect, it, vi } from 'vitest';
import { chatEntries } from './chat';
import { foldTranscript } from './conversation';
import {
  emptyFindings,
  findingRow,
  noticeFindings,
  onFindingChange,
  announceFindingChange,
  SessionFindings,
  STATEMENT_CHARS,
} from './findingRows';
import type { RunEvent } from './api-types';
import type { Finding, FindingStatus, LatestRecheck } from './reviews';

const finding = (more: Partial<Finding> = {}): Finding => ({
  finding_id: 'finding-1',
  review_id: 'review-1',
  severity: 'fail',
  category: 'unsupported_claim',
  statement: 'The bound in claim C3 does not follow.',
  remedy: null,
  status: 'open',
  revision: 2,
  reflags: 0,
  anchors: [],
  ...more,
});

const recheck = (answer: LatestRecheck['answer'], why = ''): LatestRecheck => ({
  review: 'review-2',
  answer,
  why,
  anchors: [],
  places: [],
  messages: [],
  reviewer: { alias: null, resolved_model: null, rubric: 'r' },
  at: 0,
});

describe('noticeFindings', () => {
  it('reads <finding>-r<revision>, each finding once at its latest revision', () => {
    expect(noticeFindings(['finding-a-r2', 'finding-b-r1', 'finding-a-r3', 'nonsense'])).toEqual([
      { finding: 'finding-a', revision: 3 },
      { finding: 'finding-b', revision: 1 },
    ]);
    expect(noticeFindings(['finding-r0', '-r2'])).toEqual([]);
  });
});

describe('the transcript', () => {
  const event = (seq: number, kind: RunEvent['kind'], data: Record<string, unknown>) =>
    ({ seq, at: '2026-10-25T10:00:00Z', kind, data, pins: [] }) as RunEvent;
  it('makes a reviews notice a findings row between work groups', () => {
    const events = [
      event(1, 'assistant', { text: '', tool_calls: [{ id: 'c1', name: 'shell', arguments: '' }] }),
      event(2, 'tool', { call_id: 'c1', name: 'shell', outcome: 'ok', result: '' }),
      event(3, 'notice', { source: 'reviews', ids: ['finding-1-r2', 'finding-2-r1'] }),
      event(4, 'notice', { source: 'jobs', ids: ['job-1'] }),
      event(5, 'assistant', { text: '', tool_calls: [{ id: 'c2', name: 'shell', arguments: '' }] }),
    ];
    const items = foldTranscript(chatEntries(events), events);
    expect(items.map((item) => item.kind)).toEqual(['work', 'findings', 'background', 'work']);
    expect(items[1]).toMatchObject({
      kind: 'findings',
      seq: 3,
      findings: [
        { finding: 'finding-1', revision: 2 },
        { finding: 'finding-2', revision: 1 },
      ],
    });
  });
  it('leaves a notice of another source as it was', () => {
    const events = [event(1, 'notice', { source: 'requests', ids: ['finding-1-r2'] })];
    expect(foldTranscript(chatEntries(events), events)).toEqual([]);
  });
});

describe('findingRow', () => {
  const statuses: FindingStatus[] = ['open', 'fixed', 'dismissed', 'superseded', 'stale'];
  it('gives each of the five statuses its own chip and class', () => {
    const rows = statuses.map((status) => findingRow('p1', 's1', finding({ status }), 2));
    expect(rows.map((row) => row.chip)).toEqual([
      'Open',
      'Fixed',
      'Dismissed',
      'Superseded',
      'Stale',
    ]);
    expect(new Set(rows.map((row) => row.chipClass)).size).toBe(5);
    expect(rows[4]!.chipClass).toBe('status-stale');
  });
  it('says an open finding whose last re-check could not decide is so', () => {
    const row = findingRow(
      'p1',
      's1',
      finding({ latest_recheck: recheck('inconclusive', 'The re-check was capped.') }),
      2,
    );
    expect(row.chip).toBe('Open, last re-check inconclusive');
    expect(row.recheck).toBe('open, last re-check inconclusive: The re-check was capped.');
  });
  it("gives a decided re-check's answer", () => {
    const row = findingRow(
      'p1',
      's1',
      finding({ status: 'fixed', latest_recheck: recheck('resolved') }),
      2,
    );
    expect(row.recheck).toBe('Last re-check resolved');
    expect(findingRow('p1', 's1', finding(), 2).recheck).toBeNull();
  });
  it('links a ledger anchor to the entry at its revision, and a document block to it', () => {
    const row = findingRow(
      'p1',
      's1',
      finding({
        anchors: [
          { locator: { entry: 'ent-7', handle: 'C3', revision: 4, run: 'run-1' } },
          { locator: { document: 'doc-1', revision: 3, block: 'b9' } },
        ],
      }),
      2,
    );
    expect(row.anchors[0]).toEqual({
      text: 'ledger entry C3 revision 4',
      href: '#/projects/p1/ledger/ent-7/revisions/4',
    });
    expect(row.anchors[1]!.text).toBe('document doc-1 revision 3, block b9');
    expect(row.anchors[1]!.href).toContain('/documents/doc-1/revisions/3');
  });
  it('links the successor and the full card in the review panel', () => {
    const row = findingRow(
      'p1',
      's1',
      finding({ status: 'superseded', superseded_by: 'finding-9' }),
      2,
    );
    expect(row.successor).toEqual({
      id: 'finding-9',
      href: '#/projects/p1/sessions/s1/findings/finding-9',
    });
    expect(row.card).toBe('#/projects/p1/sessions/s1/findings/finding-1');
  });
  it("shows the reviewer's hidden characters as code points, and cuts a long statement", () => {
    expect(findingRow('p1', 's1', finding({ statement: 'safe‮evil' }), 2).statement).toBe(
      'safe⟦U+202E⟧evil',
    );
    const long = findingRow('p1', 's1', finding({ statement: 'x'.repeat(2000) }), 2);
    expect(long.cut).toBe(true);
    expect(long.statement).toHaveLength(STATEMENT_CHARS);
  });
  it('says when the finding moved on from the revision offered', () => {
    expect(findingRow('p1', 's1', finding({ revision: 3 }), 2).revised).toBe(true);
    expect(findingRow('p1', 's1', finding({ revision: 2 }), 2).revised).toBe(false);
  });
});

describe('SessionFindings', () => {
  it('reads each finding once, and the latest again while the session is live', async () => {
    const state = emptyFindings();
    const read = vi.fn(async (id: string) => finding({ finding_id: id }));
    const wanted = vi.fn();
    const findings = new SessionFindings(state, read, wanted);
    findings.want(['finding-1', 'finding-2']);
    findings.want(['finding-1']);
    expect(wanted).toHaveBeenCalledTimes(1);
    await findings.refresh(false);
    expect(read).toHaveBeenCalledTimes(2);
    await findings.refresh(false);
    expect(read).toHaveBeenCalledTimes(2);
    read.mockImplementation(async (id: string) => finding({ finding_id: id, status: 'fixed' }));
    await findings.refresh(true);
    expect(read).toHaveBeenCalledTimes(4);
    expect(state.found['finding-1']!.status).toBe('fixed');
  });
  it('reads them all again after a change, and keeps a failure beside the row', async () => {
    const state = emptyFindings();
    const read = vi.fn(async (): Promise<Finding> => {
      throw new Error('not found');
    });
    const findings = new SessionFindings(state, read);
    findings.want(['finding-1']);
    await findings.refresh(false);
    expect(state.failed['finding-1']).toBe('not found');
    read.mockImplementation(async () => finding());
    const off = onFindingChange('p1', 's1', () => findings.invalidate());
    announceFindingChange('p1', 'other');
    await findings.refresh(false);
    expect(read).toHaveBeenCalledTimes(1);
    announceFindingChange('p1', 's1');
    await findings.refresh(false);
    off();
    expect(state.found['finding-1']!.status).toBe('open');
    expect(state.failed['finding-1']).toBeUndefined();
  });
});
