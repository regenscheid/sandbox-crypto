// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ChatRecovery from '../views/ChatRecovery.svelte';
import type { StatusPage } from './api-types';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

const status = (active: boolean): StatusPage => ({
  root: 'work-root',
  unfinished: 1,
  active,
  works: [],
  resumes: [],
  next_work: null,
  next_resume: null,
  pending_kernel_restore_work: null,
  pending_kernel_restore_identity: null,
});

async function show(active: boolean) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) =>
      Response.json(
        url.endsWith('/recovery') ? status(active) : { requests: [], next_after: null },
      ),
    ),
  );
  const target = document.createElement('div');
  document.body.append(target);
  const props = { projectId: 'p', sessionId: 's', held: false, busy: false, closed: false };
  mounted.push(mount(ChatRecovery, { target, props: { ...props, resume: async () => {} } }));
  await settle();
  return target;
}

const said = (target: HTMLElement) => (target.textContent ?? '').replace(/\s+/g, ' ');

it('shows nothing, and no Resume button, while research is active', async () => {
  // A disabled Resume button beside "Research is active" read as something to do
  // (owner, 2026-10-21).
  const target = await show(true);
  expect(said(target)).not.toContain('stopped');
  expect(target.querySelector('button')).toBeNull();
});

it('offers Resume when the unfinished research has stopped', async () => {
  const target = await show(false);
  expect(said(target)).toContain('One line of research stopped before finishing.');
  const resume = target.querySelector('button');
  expect(resume?.textContent).toBe('Resume research');
  expect(resume?.disabled).toBe(false);
});
