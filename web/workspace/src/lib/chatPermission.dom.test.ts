// SPDX-License-Identifier: MIT
// A request held for the user's answer (D76, D97): the card shows a structured summary of
// what is asked, the call's arguments from the transcript folded away, only the scopes the
// request's ceiling allows, and each choice sends its decision.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ChatPermission from '../views/ChatPermission.svelte';
import type { PendingApproval, RunEvent } from './api-types';
import { awaitingApproval, chatEntries } from './chat';
import { heldCall } from './conversation';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
});

const request: PendingApproval = {
  seq: 12,
  server: 'crypto-research-mcp',
  tool: 'web_fetch',
  method: 'tools/call',
  policy: 'sha256:' + 'a'.repeat(64),
  session: 'main',
  run: null,
  rule: 'mcp.servers.crypto-research-mcp.tools.escalate[web_fetch]',
  reason: 'escalated',
  recorded_at: '2026-10-21T18:00:00Z',
  expires_at: '2026-10-21T18:30:00Z',
  timed_out: false,
  on_timeout: 'deny',
  child: null,
  kind: 'mcp',
  source: 'proxy',
  caller_kind: 'agent',
  effect: 'write',
  max_grant: 'project',
  summary: { url: 'https://www.washingtonpost.com/', host: 'www.washingtonpost.com' },
  http: null,
  packages: null,
  hold: null,
  scopes: ['once', 'session', 'project'],
};

const network: PendingApproval = {
  ...request,
  seq: 30,
  kind: 'network',
  source: 'worker',
  server: '',
  tool: '',
  method: 'POST',
  caller_kind: 'code',
  effect: null,
  max_grant: 'once',
  summary: {},
  http: {
    method: 'POST',
    host: 'api.example.org',
    port: 443,
    path: '/v1/items',
    size: 41,
    content_type: 'application/json',
    fields: { title: 'A draft', tags: '[2 items]' },
  },
  scopes: ['once'],
};

function card(
  call: { arguments: string; text: string } | null = null,
  asked: PendingApproval = request,
) {
  const sent: string[] = [];
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(ChatPermission, {
      target,
      props: {
        request: asked,
        call,
        answer: vi.fn(async (decision: string) => {
          sent.push(decision);
        }),
      },
    }),
  );
  flushSync();
  return { target, sent };
}

const button = (root: HTMLElement, words: string) =>
  [...root.querySelectorAll('button')].find((b) => b.textContent?.trim().startsWith(words))!;
const settle = async () => {
  for (let i = 0; i < 4; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
};

describe('a held tool call', () => {
  it('asks about the tool, with the agent words marked as its own and the arguments', () => {
    const { target } = card({
      arguments: '{"url": "https://www.washingtonpost.com/", "max_chars": 12000}',
      text: 'Fetching the homepage.',
    });
    expect(target.textContent).toContain('Use web_fetch?');
    expect(target.textContent).toContain('crypto-research-mcp');
    expect(target.textContent).toContain('Agent-supplied reason.');
    expect(target.querySelector('pre')!.textContent).toContain('"max_chars": 12000');
  });

  it('shows the significant arguments and the effect class', () => {
    const { target } = card();
    const summary = target.querySelector('.summary')!.textContent!;
    expect(summary).toContain('www.washingtonpost.com');
    expect(summary).toContain('write');
  });

  it('allows once by default, for the session or the project when chosen, or denies', async () => {
    const { target, sent } = card();
    button(target, 'Allow').click();
    await settle();
    button(target, 'Deny').click();
    await settle();
    target.querySelector<HTMLButtonElement>('[aria-label="Choose how long to allow"]')!.click();
    flushSync();
    expect(target.textContent).toContain('Any write call to crypto-research-mcp');
    button(target, 'This session').click();
    flushSync();
    expect(button(target, 'Allow').textContent).toContain('This session');
    button(target, 'Allow').click();
    await settle();
    target.querySelector<HTMLButtonElement>('[aria-label="Choose how long to allow"]')!.click();
    flushSync();
    button(target, 'This project').click();
    flushSync();
    button(target, 'Allow').click();
    await settle();
    expect(sent).toEqual(['once', 'deny', 'session', 'project']);
  });

  it('offers only the scopes the ceiling allows', () => {
    const { target } = card(null, { ...request, effect: 'unclassified', scopes: ['once'] });
    expect(target.querySelector('[aria-label="Choose how long to allow"]')).toBeNull();
    expect(button(target, 'Allow').textContent).toContain('once');
  });
});

describe("research code's network request (D97)", () => {
  it('shows method, host, path, content type, size and the body fields folded', async () => {
    const { target, sent } = card(null, network);
    expect(target.textContent).toContain('Let research code POST api.example.org?');
    expect(target.textContent).toContain('Asked by research code.');
    const rows = target.querySelector('.summary')!.textContent!;
    for (const part of ['/v1/items', 'application/json', '41 bytes', 'api.example.org:443'])
      expect(rows).toContain(part);
    const folded = target.querySelector('details')!;
    expect(folded.open).toBe(false);
    expect(folded.textContent).toContain('A draft');
    expect(target.querySelector('[aria-label="Choose how long to allow"]')).toBeNull();
    button(target, 'Allow').click();
    await settle();
    expect(sent).toEqual(['once']);
  });

  it("shows a package request's sources and the agent's words", () => {
    const { target } = card(null, {
      ...network,
      kind: 'packages',
      http: null,
      caller_kind: 'agent',
      max_grant: 'project',
      packages: {
        sources: [{ ecosystem: 'conda', channel: 'conda-forge' }],
        files: 3,
        asked: 'Conda: sage',
        purpose: 'Sage for the lattice estimates.',
        package: null,
      },
      scopes: ['once', 'session', 'project'],
    });
    expect(target.textContent).toContain('Install these packages?');
    expect(target.textContent).toContain('conda (conda-forge)');
    expect(target.textContent).toContain('Sage for the lattice estimates.');
    expect(target.textContent).toContain('Agent-supplied reason.');
  });
});

describe("a delegated agent's held call (D99)", () => {
  it('names the child, shows its brief and its own words, and still asks the user', () => {
    const { target } = card(
      { arguments: '{}', text: 'The main run said this.' },
      {
        ...request,
        child: {
          delegation_id: 'dlg-' + '0'.repeat(24),
          run: 'child-run',
          kind: 'task',
          brief: 'Find the SNOVA parameter sets.',
          words: 'Fetching the round-2 page.',
        },
      },
    );
    expect(target.textContent).toContain('Asked by a delegated agent (task)');
    expect(target.querySelector('.brief')!.textContent).toContain('Find the SNOVA parameter sets.');
    expect(target.textContent).toContain('Fetching the round-2 page.');
    expect(target.textContent).not.toContain('The main run said this.');
    expect(button(target, 'Allow')).toBeTruthy();
  });

  it("shows a direct-path child's brief without borrowing the main run's words", () => {
    const { target } = card(
      { arguments: '{}', text: 'The main run said this.' },
      {
        ...request,
        run: 'child-run',
        child: {
          delegation_id: 'dlg-' + '0'.repeat(24),
          run: 'child-run',
          kind: 'check',
          brief: 'Check the entry.',
          words: '',
        },
      },
    );
    expect(target.textContent).toContain('Asked by a delegated agent (check)');
    expect(target.querySelector('.brief')!.textContent).toContain('Check the entry.');
    expect(target.textContent).not.toContain('The main run said this.');
  });
});

describe('the transcript of a held call', () => {
  const event = (seq: number, kind: RunEvent['kind'], data: Record<string, unknown>): RunEvent =>
    ({ seq, at: '2026-10-21T18:00:00Z', kind, data, pins: [] }) as RunEvent;
  const events = [
    event(1, 'assistant', {
      text: 'Fetching.',
      tool_calls: [{ id: 'c1', name: 'web_fetch', arguments: '{"url": "a"}' }],
    }),
    event(2, 'waiting', {
      permission_request: 12,
      server: 'crypto-research-mcp',
      tool: 'web_fetch',
    }),
  ];

  it('finds the held call and says the run waits for the user', () => {
    expect(heldCall(events, 'web_fetch')).toEqual({ arguments: '{"url": "a"}', text: 'Fetching.' });
    expect(heldCall([...events, event(3, 'tool', { call_id: 'c1' })], 'web_fetch')).toBeNull();
    expect(awaitingApproval({ run: 'r', state: 'active', events })).toBe('web_fetch');
    const ended = [...events, event(3, 'waiting', { permission_request: null })];
    expect(awaitingApproval({ run: 'r', state: 'active', events: ended })).toBeNull();
    expect(chatEntries(ended).map((entry) => ('text' in entry ? entry.text : ''))).toContain(
      'The wait for approval ended.',
    );
  });
});
