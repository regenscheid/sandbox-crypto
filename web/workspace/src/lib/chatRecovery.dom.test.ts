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

const status = (active: boolean, resumes: StatusPage['resumes'] = []): StatusPage => ({
  root: 'work-root',
  unfinished: 1,
  active,
  works: [],
  resumes,
  next_work: null,
  next_resume: null,
  pending_kernel_restore_work: null,
  pending_kernel_restore_identity: null,
});

async function show(active: boolean, resumes: StatusPage['resumes'] = []) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) =>
      Response.json(
        url.endsWith('/recovery') ? status(active, resumes) : { requests: [], next_after: null },
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

it('says why the last resume did not start', async () => {
  // Every Resume failed on the Mac with no reason shown (2026-10-21).
  const target = await show(false, [
    {
      request: 'resume-1',
      root: 'work-root',
      status: 'blocked',
      reason: "interrupted: environment claim's policy is stale",
      created: 1,
    },
    { request: 'resume-0', root: 'work-root', status: 'finished', reason: null, created: 0 },
  ]);
  expect(said(target)).toContain(
    "The last resume did not start: interrupted: environment claim's policy is stale",
  );
});
