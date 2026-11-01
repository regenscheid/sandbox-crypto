// SPDX-License-Identifier: MIT
// A hold of the session's outward calls (D103): the card says what the detection rule saw
// and lists the latest calls; Clear hold and Stop session each send their decision.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ChatHold from '../views/ChatHold.svelte';
import type { PendingApproval } from './api-types';
import { holdSummary } from './permissionCards';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
});

const hold: PendingApproval = {
  seq: 41,
  kind: 'hold',
  source: 'worker',
  run: null,
  server: '',
  tool: '',
  method: '',
  policy: 'sha256:' + 'a'.repeat(64),
  session: null,
  rule: 'detect.host-rate',
  reason: '301 calls to example.org within 60 s',
  recorded_at: '2026-10-24T12:00:00Z',
  expires_at: '2026-11-23T12:00:00Z',
  timed_out: false,
  on_timeout: 'deny',
  child: null,
  caller_kind: 'code',
  effect: null,
  max_grant: 'once',
  summary: {},
  http: null,
  packages: null,
  hold: {
    rule: 'host-rate',
    count: 301,
    threshold: 300,
    window_seconds: 60,
    host: 'example.org',
    requests: [{ method: 'GET', host: 'example.org', target: '/items/7?(5 bytes)' }],
  },
  scopes: [],
};

describe('a hold of the session', () => {
  it('says what the rule saw, in words', () => {
    expect(holdSummary(hold)).toBe(
      'Paused for a sustained burst of calls to one host: 301 calls to example.org within ' +
        '60 seconds, over the threshold of 300.',
    );
  });

  it('lists the latest calls, and clears or stops', async () => {
    const sent: string[] = [];
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(
      mount(ChatHold, {
        target,
        props: {
          request: hold,
          answer: vi.fn(async (decision: string) => {
            sent.push(decision);
          }),
        },
      }),
    );
    flushSync();
    expect(target.textContent).toContain('Outward calls paused');
    expect(target.textContent).toContain('/items/7?(5 bytes)');
    const button = (words: string) =>
      [...target.querySelectorAll('button')].find((b) => b.textContent?.trim() === words)!;
    button('Clear hold').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    button('Stop session').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(sent).toEqual(['clear', 'stop']);
  });
});
