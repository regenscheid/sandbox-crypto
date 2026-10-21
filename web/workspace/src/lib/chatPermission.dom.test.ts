// SPDX-License-Identifier: MIT
// A connector tool call held for the user's answer (D76): the card shows what is asked
// and the call's arguments from the transcript, and each choice sends its decision.
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
  session: 'main',
  rule: 'mcp.servers.crypto-research-mcp.tools.escalate[web_fetch]',
  reason: 'escalated',
  recorded_at: '2026-10-21T18:00:00Z',
  expires_at: '2026-10-21T18:30:00Z',
  timed_out: false,
  on_timeout: 'deny',
};

function card(call: { arguments: string; text: string } | null = null) {
  const sent: string[] = [];
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(ChatPermission, {
      target,
      props: {
        request,
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

  it('allows once by default, for the project or the server when chosen, or denies', async () => {
    const { target, sent } = card();
    button(target, 'Allow').click();
    await settle();
    button(target, 'Deny').click();
    await settle();
    target.querySelector<HTMLButtonElement>('[aria-label="Choose how long to allow"]')!.click();
    flushSync();
    button(target, 'Any tool in crypto-research-mcp').click();
    flushSync();
    expect(button(target, 'Allow').textContent).toContain('Any tool in crypto-research-mcp');
    button(target, 'Allow').click();
    await settle();
    target.querySelector<HTMLButtonElement>('[aria-label="Choose how long to allow"]')!.click();
    flushSync();
    button(target, 'This project').click();
    flushSync();
    button(target, 'Allow').click();
    await settle();
    expect(sent).toEqual(['once', 'deny', 'server', 'project']);
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
