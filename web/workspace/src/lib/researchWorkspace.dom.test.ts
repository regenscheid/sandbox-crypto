// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import SessionView from '../views/SessionView.svelte';
import PaneResizer from '../views/PaneResizer.svelte';
import { focusPanel } from './focusPanel';
const mounted: ReturnType<typeof mount>[] = [];
afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});
async function settle() {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}
function session(reviewId: string | null = null) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      let data: unknown;
      if (url.endsWith('/sessions/s1'))
        data = {
          project_id: 'p1',
          session: {
            session_id: 's1',
            environment_id: 'e1',
            dedicated: false,
            opened_at: '2026-10-17T12:00:00Z',
            closed_at: null,
          },
          runs: [],
        };
      else if (url.includes('/messages'))
        data = { messages: [], run: null, service: false, environment: null };
      else if (url.includes('/questions')) data = { questions: [], run: null };
      else if (url.endsWith('/plan'))
        data = {
          project_id: 'p1',
          session_id: 's1',
          observed_at: '2026-10-17T12:00:00Z',
          session_closed_at: null,
          plan: null,
        };
      else if (url.endsWith('/documents')) data = { documents: [] };
      else if (url.includes('/uploads')) data = { uploads: [], next_after: null };
      else if (url.includes('/artifacts')) data = { artifacts: [], next_offset: null };
      else if (url.includes('/reviews')) return new Response('{}', { status: 503 });
      else if (url.includes('/fold-proposals')) data = { proposals: [] };
      else if (url.includes('/jobs')) data = { jobs: [] };
      return data === undefined
        ? new Response('{}', { status: 503 })
        : new Response(JSON.stringify(data));
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(SessionView, { target, props: { projectId: 'p1', sessionId: 's1', reviewId } }),
  );
  flushSync();
  return target;
}
it('keeps the composer draft while switching and closing research views', async () => {
  vi.stubGlobal('innerWidth', 1400);
  const root = session();
  await settle();
  const composer = root.querySelector('textarea')!;
  composer.value = 'Preserve $x^2$ while checking the plan.';
  composer.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  for (const panel of ['documents', 'plan', 'plan']) {
    root.querySelector<HTMLButtonElement>(`button[data-panel="${panel}"]`)!.click();
    await settle();
    expect(root.querySelector('textarea')).toBe(composer);
    expect(composer.value).toContain('Preserve $x^2$');
  }
  expect(root.querySelector<HTMLElement>('#research-pane')!.hidden).toBe(true);
});
it('a review deep link does not force Activity back after switching tabs', async () => {
  vi.stubGlobal('innerWidth', 1400);
  const root = session('review-1');
  await settle();
  root.querySelector<HTMLButtonElement>('button[data-panel="documents"]')!.click();
  await settle();
  expect(root.querySelector('button[data-panel="documents"]')!.getAttribute('aria-pressed')).toBe(
    'true',
  );
});
it('opens compact panes as dialogs and Escape restores focus to the trigger', async () => {
  vi.stubGlobal('innerWidth', 600);
  const root = session();
  await settle();
  const trigger = root.querySelector<HTMLButtonElement>('button[data-panel="plan"]')!;
  trigger.focus();
  trigger.click();
  await settle();
  const pane = root.querySelector<HTMLElement>('#research-pane')!;
  expect(pane.getAttribute('aria-modal')).toBe('true');
  expect(pane.contains(document.activeElement)).toBe(true);
  document.activeElement!.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
  );
  await settle();
  expect(pane.hidden).toBe(true);
  expect(document.activeElement).toBe(trigger);
});
it('contains focus across closed details and hidden controls and restores it on close', async () => {
  document.body.innerHTML =
    '<button id="outside">Open</button><aside tabindex="-1"><button id="first">Close</button><details><summary tabindex="0">History</summary><button>Hidden action</button></details><div hidden><button>Hidden tab</button></div></aside>';
  const outside = document.querySelector<HTMLButtonElement>('#outside')!;
  const pane = document.querySelector('aside')!;
  const close = vi.fn();
  outside.focus();
  const action = focusPanel(pane, { active: true, close });
  await settle();
  const first = pane.querySelector<HTMLButtonElement>('#first')!;
  const summary = pane.querySelector('summary')!;
  first.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }),
  );
  expect(document.activeElement).toBe(summary);
  summary.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }),
  );
  expect(document.activeElement).toBe(first);
  outside.focus();
  expect(document.activeElement).toBe(first);
  action.update({ active: false, close });
  await settle();
  expect(document.activeElement).toBe(outside);
  action.destroy();
});
it('resizes with the keyboard within its declared bounds', () => {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(PaneResizer, { target }));
  flushSync();
  const separator = target.querySelector('[role="separator"]')!;
  for (const [key, value] of [
    ['ArrowLeft', '484'],
    ['Home', '320'],
    ['ArrowRight', '320'],
    ['End', '760'],
    ['ArrowLeft', '760'],
  ]) {
    separator.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
    flushSync();
    expect(separator.getAttribute('aria-valuenow')).toBe(value);
  }
});

it('gives focus to navigation when the simultaneously activated inspector has an inert ancestor', async () => {
  document.body.innerHTML =
    '<aside id="navigation" tabindex="-1"><button>Close navigation</button><a href="#/">Sessions</a></aside><main inert><aside id="inspector" tabindex="-1"><button>Close inspector</button></aside></main>';
  const navigation = document.querySelector<HTMLElement>('#navigation')!;
  const inspector = document.querySelector<HTMLElement>('#inspector')!;
  const nav = focusPanel(navigation, { active: true, close: vi.fn() });
  const pane = focusPanel(inspector, { active: true, close: vi.fn() });
  await settle();
  expect(navigation.contains(document.activeElement)).toBe(true);
  navigation
    .querySelector('button')!
    .dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }),
    );
  expect(document.activeElement).toBe(navigation.querySelector('a'));
  navigation.hidden = true;
  document.querySelector('main')!.inert = false;
  nav.update({ active: false, close: vi.fn() });
  await settle();
  expect(inspector.contains(document.activeElement)).toBe(true);
  nav.destroy();
  pane.destroy();
});
