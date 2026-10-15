// SPDX-License-Identifier: MIT
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError, type LedgerChangeAnswer } from './api';
import {
  changeFlags,
  clearedSummary,
  clearedText,
  flagDetails,
  flagSource,
  openAgain,
  openFlagsText,
  reasonProblem,
  reopenedSummary,
} from './ledgerFlags';
import { idle } from './submit';

afterEach(() => vi.unstubAllGlobals());

const answer: LedgerChangeAnswer = {
  operation_id: 'op',
  replayed: false,
  entries: [],
  cleared_total: 1,
  flags_cleared: ['flag-1'],
  flags_reopened: [],
};

describe('a flag', () => {
  it('leads to the entry it came from, at the revision that raised it', () => {
    expect(
      flagSource('p 1', { entry: 'entry-0', handle: 'A-1', revision: 3, status: 'withdrawn' }),
    ).toEqual({ text: 'A-1 r3 (withdrawn)', href: '#/projects/p%201/ledger/A-1/revisions/3' });
    expect(flagSource('p1', { entry: 'entry-0', handle: 'C-2' })).toEqual({
      text: 'C-2',
      href: '#/projects/p1/ledger/C-2',
    });
  });

  it('from a source of another shape shows it as stored, leading nowhere', () => {
    for (const source of [
      { handle: 'javascript:alert(1)' },
      { handle: 'C-1/../x' },
      { entry: 'entry-0' },
    ]) {
      expect(flagSource('p1', source)).toEqual({ text: JSON.stringify(source), href: null });
    }
    expect(flagSource('p1', { handle: 'C-1', revision: 1.5 }).href).toBe(
      '#/projects/p1/ledger/C-1',
    );
  });

  it('says the revision it was raised on, what it spread from or reopens, and the reviews', () => {
    expect(flagDetails({ revision: 2, via: null, reviews: [], reopens: null })).toBe(
      'raised on r2',
    );
    expect(
      flagDetails({ revision: 4, via: 'flag-9', reviews: ['rev-1', 'rev-2'], reopens: 'flag-3' }),
    ).toBe(
      'raised on r4; spread from flag flag-9; reopens flag flag-3; held by reviews rev-1, rev-2',
    );
  });

  it('counts on a card or a row, and nothing for none', () => {
    expect(openFlagsText(0)).toBe('');
    expect(openFlagsText(1)).toBe('1 open flag');
    expect(openFlagsText(7)).toBe('7 open flags');
  });

  it('cleared says who cleared it, when and why', () => {
    expect(clearedText({ reason: 'still holds' }, 'the user', 'today')).toBe(
      'Cleared today by the user: still holds',
    );
  });
});

it('a reason is required, and bounded', () => {
  expect(reasonProblem('  \n')).toMatch(/Give the reason/);
  expect(reasonProblem('x'.repeat(4097))).toMatch(/longer than 4096/);
  expect(reasonProblem('The change does not affect it.')).toBeNull();
});

describe("the user's clearing and reopening", () => {
  it('send the change with an operation ID, then read the entry again', async () => {
    const reload = vi.fn().mockResolvedValue(undefined);
    const send = vi.fn().mockResolvedValue(answer);
    const saving = idle();
    const change = { flags: ['flag-1'], reason: 'Still holds.' };
    expect(await changeFlags(saving, 'clear', 'p1', 'entry-1', change, send, reload)).toEqual(
      answer,
    );
    expect(send.mock.calls[0]?.[0]).toEqual({ ...change, operation_id: expect.any(String) });
    expect(reload).toHaveBeenCalledTimes(1);
    expect(saving).toEqual({ busy: false, error: null });
  });

  it('keep a conflict or a refusal, read the entry again, and retry with the same ID', async () => {
    const reload = vi.fn().mockResolvedValue(undefined);
    for (const [status, kind] of [
      [409, 'conflict'],
      [400, 'invalid_request'],
      [404, 'not_found'],
    ] as const) {
      const saving = idle();
      const refused = vi.fn().mockRejectedValue(new ApiError(status, kind, `refused ${status}`));
      const change = { flags: ['flag-1', 'flag-2'], reason: `Both, ${status}.` };
      expect(
        await changeFlags(saving, 'clear', 'p1', 'entry-1', change, refused, reload),
      ).toBeNull();
      expect((saving.error as ApiError).status).toBe(status);
      await changeFlags(saving, 'clear', 'p1', 'entry-1', change, refused, reload);
      expect(refused.mock.calls[1]?.[0].operation_id).toBe(refused.mock.calls[0]?.[0].operation_id);
    }
    expect(reload).toHaveBeenCalledTimes(6);
  });

  it('keep the IDs of a clearing and a reopening apart', async () => {
    const send = vi.fn().mockResolvedValue(answer);
    const fail = vi.fn().mockRejectedValue(new ApiError(409, 'conflict', 'x'));
    const change = { flags: ['flag-1'], reason: 'Same words.' };
    await changeFlags(idle(), 'clear', 'p1', 'e', change, fail, async () => {});
    await changeFlags(idle(), 'reopen', 'p1', 'e', change, send, async () => {});
    expect(send.mock.calls[0]?.[0].operation_id).not.toBe(fail.mock.calls[0]?.[0].operation_id);
  });

  it('post to the entry, its flags and its reopened flags', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify(answer)));
    vi.stubGlobal('fetch', fetch);
    await api.clearLedgerFlags('p/1', 'C-1', {
      flags: ['flag-1'],
      reason: 'r',
      operation_id: 'o1',
    });
    await api.reopenLedgerFlags('p/1', 'C-1', {
      flags: ['flag-1'],
      reason: 'r',
      operation_id: 'o2',
    });
    const calls = fetch.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map(([url]) => url)).toEqual([
      '/api/v0/projects/p%2F1/ledger/C-1/flags',
      '/api/v0/projects/p%2F1/ledger/C-1/flags/reopened',
    ]);
    expect(calls.map(([, init]) => init.method)).toEqual(['POST', 'POST']);
    expect(JSON.parse(String(calls[0]![1].body))).toEqual({
      flags: ['flag-1'],
      reason: 'r',
      operation_id: 'o1',
    });
  });
});

describe('what a change did', () => {
  it('says how many flags a clearing cleared, and when that is not how many were selected', () => {
    expect(clearedSummary({ cleared_total: 1 }, 1)).toBe('Cleared 1 flag.');
    expect(clearedSummary({ cleared_total: 3 }, 3)).toBe('Cleared 3 flags.');
    expect(clearedSummary({ cleared_total: 2 }, 3)).toBe(
      'Cleared 2 flags, though 3 were selected: read the flags again before going on.',
    );
  });

  it('says how many flags a reopening opened again', () => {
    expect(reopenedSummary({ flags_reopened: ['flag-9'] })).toBe(
      'Reopened 1 flag, each as a new open flag.',
    );
  });
});

describe('a cleared flag', () => {
  const cleared = { flag_id: 'flag-1', cause: 'spread' as const, source: { handle: 'A-1' } };

  it('stands open again through the flag that reopened it, or one of its cause and source', () => {
    expect(openAgain(cleared, [])).toBeNull();
    expect(
      openAgain(cleared, [{ flag_id: 'flag-2', cause: 'spread', source: {}, reopens: 'flag-1' }]),
    ).toBe('flag-2');
    expect(
      openAgain(cleared, [
        { flag_id: 'flag-3', cause: 'spread', source: { handle: 'A-1' }, reopens: null },
      ]),
    ).toBe('flag-3');
    expect(
      openAgain(cleared, [
        { flag_id: 'flag-4', cause: 'dependency_status', source: { handle: 'A-1' }, reopens: null },
      ]),
    ).toBeNull();
  });
});
