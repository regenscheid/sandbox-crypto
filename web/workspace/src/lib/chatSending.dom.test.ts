// SPDX-License-Identifier: MIT
// A sent message shows at once, as "Sending…", and the chat reads it back right after the
// write rather than at its next poll (owner, 2026-10-21: a message took a while to appear).
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ChatPanel from '../views/ChatPanel.svelte';
import type { ChatMessage, MessageListing } from './api-types';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

it('shows the message while it is sent, then as the chat reads it back', async () => {
  const written: ChatMessage[] = [];
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  const reads: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        await held;
        const body = JSON.parse(String(init.body)) as { message_id: string; text: string };
        const message = {
          message_id: body.message_id,
          text: body.text,
          author: 'user',
          state: 'queued',
          created_at: '2026-10-22T02:40:00Z',
        } as unknown as ChatMessage;
        written.push(message);
        return new Response(JSON.stringify({ message, question: null, run: null }));
      }
      if (url.includes('/messages')) {
        reads.push(url);
        const listing: MessageListing = {
          messages: written,
          run: null,
          service: true,
          environment: null,
        };
        return new Response(JSON.stringify(listing));
      }
      if (url.includes('/questions'))
        return new Response(JSON.stringify({ questions: [], run: null }));
      if (url.includes('/recovery/resources'))
        return new Response(JSON.stringify({ requests: [], next_after: null }));
      return new Response(JSON.stringify({ state: 'active', events: [], runs: [] }));
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(ChatPanel, { target, props: { projectId: 'p1', sessionId: 's9', closed: false } }),
  );
  await settle();
  const box = target.querySelector('textarea')!;
  box.value = 'Read the papers.';
  box.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  [...target.querySelectorAll('button')]
    .find((item) => item.textContent?.trim().startsWith('Send'))!
    .click();
  flushSync();
  const sending = target.querySelector('.bubble.sending')!;
  expect(sending.textContent).toContain('Sending…');
  expect(sending.textContent).toContain('Read the papers.');
  const before = reads.length;
  release();
  await settle();
  // Read back at once, well inside the chat's 1.5-second poll.
  expect(reads.length).toBeGreaterThan(before);
  expect(target.querySelector('.bubble.sending')).toBeNull();
  expect(target.querySelector('.waiting-messages')!.textContent).toContain('Read the papers.');
});
