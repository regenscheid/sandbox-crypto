// SPDX-License-Identifier: MIT
// The platform's automatic continuation of an open plan is folded to "Continuation" on the
// user's side of the conversation, its whole prompt one click away (owner, 2026-10-21).
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it } from 'vitest';
import ChatRun from '../views/ChatRun.svelte';
import type { RunEvent } from './api-types';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
});

const event = (seq: number, kind: RunEvent['kind'], data: Record<string, unknown>): RunEvent =>
  ({ seq, at: '2026-10-22T02:20:00Z', kind, data, pins: [] }) as RunEvent;

it("folds a continuation and leaves the user's own message as it is", () => {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(ChatRun, {
      target,
      props: {
        run: {
          run: 'r1',
          state: 'ended',
          events: [
            event(1, 'user', { message_id: 'msg-auto', text: 'Pursue the next step.' }),
            event(2, 'assistant', { text: 'Done.', tool_calls: [] }),
            event(3, 'user', { message_id: 'msg-user', text: 'Does anything work?' }),
          ],
        },
        continuations: new Set(['msg-auto']),
      },
    }),
  );
  flushSync();
  const folded = target.querySelector<HTMLDetailsElement>('.continuation details')!;
  expect(folded.open).toBe(false);
  expect(folded.querySelector('summary')!.textContent?.trim()).toBe('Continuation');
  expect(folded.textContent).toContain('Pursue the next step.');
  const bubbles = [...target.querySelectorAll('.user-message')].map((li) => li.textContent);
  expect(bubbles).toHaveLength(1);
  expect(bubbles[0]).toContain('Does anything work?');
});
