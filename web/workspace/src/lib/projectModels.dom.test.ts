// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ModelPicker from '../views/ModelPicker.svelte';
import ProjectModels from '../views/ProjectModels.svelte';
import type { ModelSettings } from './api-types';
import { setViewer } from './viewer.svelte';

const mounted: ReturnType<typeof mount>[] = [];
let identities = 0;
beforeEach(() => {
  identities = 0;
  vi.stubGlobal('crypto', {
    randomUUID: () => `00000000-0000-4000-8000-${String(++identities).padStart(12, '0')}`,
    subtle: {
      digest: async (_algorithm: string, bytes: Uint8Array) => {
        const digest = new Uint8Array(32);
        bytes.forEach((byte, index) => {
          const offset = index % digest.length;
          digest[offset] = (digest[offset] ?? 0) ^ byte;
        });
        return digest.buffer;
      },
    },
  });
});
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
  setViewer(null);
});

async function settle() {
  for (let index = 0; index < 6; index++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

const settings = (revision = 0): ModelSettings => ({
  project: 'p1',
  revision,
  roles: {
    main: { alias: 'sol', reasoning_effort: 'max', deployment_default: false },
    delegated: { alias: 'sol', reasoning_effort: 'max', deployment_default: false },
    reviewer: { alias: 'luna', reasoning_effort: 'medium', deployment_default: false },
  },
  defaults: {
    main: { alias: 'sol', reasoning_effort: 'max', deployment_default: false },
    delegated: { alias: 'sol', reasoning_effort: 'max', deployment_default: false },
    reviewer: { alias: 'luna', reasoning_effort: 'medium', deployment_default: false },
  },
  default_efforts: {},
  available: [
    { alias: 'luna', api: 'responses', effort_supported: true },
    { alias: 'sol', api: 'responses', effort_supported: true },
  ],
  known_efforts: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
});

it('shows all three role choices and saves an independent reviewer effort', async () => {
  const requests: RequestInit[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit = {}) => {
      if (init.method === 'POST') {
        requests.push(init);
        return new Response(JSON.stringify(settings(1)));
      }
      return new Response(JSON.stringify(settings()));
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ProjectModels, { target, props: { projectId: 'p1' } }));
  await settle();
  expect(target.textContent).toContain('Orchestrator');
  expect(target.textContent).toContain('Delegated agents');
  expect(target.textContent).toContain('Consistency reviewer');
  const selects = target.querySelectorAll<HTMLSelectElement>('select');
  expect(selects).toHaveLength(6);
  expect(selects[5]!.value).toBe('medium');
  selects[5]!.value = 'low';
  selects[5]!.dispatchEvent(new Event('change', { bubbles: true }));
  const save = [...target.querySelectorAll('button')].find((button) =>
    button.textContent?.includes('Save agent models'),
  );
  save!.click();
  await settle();
  const body = JSON.parse(String(requests[0]!.body));
  expect(body.roles.reviewer).toEqual({
    alias: 'luna',
    reasoning_effort: 'low',
    deployment_default: false,
  });
  expect(target.textContent).toContain('New research trees will use them');
});

it('keeps a default that depends on the API the run service chooses', async () => {
  const requests: RequestInit[] = [];
  const chosen = (): ModelSettings => {
    const base = settings();
    const main = { alias: 'claude', reasoning_effort: null, deployment_default: true };
    return {
      ...base,
      roles: { ...base.roles, main },
      defaults: { ...base.defaults, main },
      default_efforts: { main: { effort: 'max', messages: 'high' } },
      available: [...base.available, { alias: 'claude', api: null, effort_supported: true }],
    };
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit = {}) => {
      if (init.method === 'POST') requests.push(init);
      return new Response(JSON.stringify(chosen()));
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ProjectModels, { target, props: { projectId: 'p1' } }));
  await settle();
  const effort = target.querySelectorAll<HTMLSelectElement>('select')[1]!;
  expect(effort.value).toBe('@deployment');
  expect(effort.selectedOptions[0]!.textContent).toBe(
    'Deployment default (high on Messages, else max)',
  );
  const save = [...target.querySelectorAll('button')].find((button) =>
    button.textContent?.includes('Save agent models'),
  );
  save!.click();
  await settle();
  const body = JSON.parse(String(requests[0]!.body));
  expect(body.roles.main).toEqual({
    alias: 'claude',
    reasoning_effort: null,
    deployment_default: true,
  });
  effort.value = 'medium';
  effort.dispatchEvent(new Event('change', { bubbles: true }));
  save!.click();
  await settle();
  expect(JSON.parse(String(requests[1]!.body)).roles.main).toEqual({
    alias: 'claude',
    reasoning_effort: 'medium',
    deployment_default: false,
  });
});

it('retries an uncertain save with the exact operation and locks the pending draft', async () => {
  const posts: Record<string, unknown>[] = [];
  let release!: () => void;
  const pending = new Promise<void>((resolve) => (release = resolve));
  let failed = false;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit = {}) => {
      if (init.method === 'POST') {
        posts.push(JSON.parse(String(init.body)));
        if (!failed) {
          failed = true;
          await pending;
          throw new TypeError('connection lost after commit');
        }
        return new Response(JSON.stringify(settings(1)));
      }
      return new Response(JSON.stringify(settings(failed ? 1 : 0)));
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ProjectModels, { target, props: { projectId: 'p1' } }));
  await settle();
  const save = [...target.querySelectorAll('button')].find((button) =>
    button.textContent?.includes('Save agent models'),
  )!;
  save.click();
  await settle();
  expect([...target.querySelectorAll('select')].every((select) => select.disabled)).toBe(true);
  release();
  await settle();
  expect(target.textContent).toContain('Current revision 1');
  save.click();
  await settle();
  expect(posts).toHaveLength(2);
  expect(posts[1]!.operation_id).toBe(posts[0]!.operation_id);
  expect(posts[1]!.expected).toBe(0);
});

it('disables saving the models for a user outside the admin group (D104)', async () => {
  setViewer({ user: 'bob', login: 'forward', admin: false, admin_group: 'atlas-admins' });
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify(settings()))),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ProjectModels, { target, props: { projectId: 'p1' } }));
  await settle();
  const save = [...target.querySelectorAll<HTMLButtonElement>('button')].find(
    (item) => item.textContent?.trim() === 'Save agent models',
  )!;
  expect(save.disabled).toBe(true);
  expect(save.title).toContain('atlas-admins');
  expect(target.querySelector('.admin-only')?.textContent).toContain('decide spend');
});

it('lets the composer picker keep or replace an API-dependent default', async () => {
  const requests: RequestInit[] = [];
  const base = settings();
  const main = { alias: 'claude', reasoning_effort: null, deployment_default: true };
  const chosen: ModelSettings = {
    ...base,
    roles: { ...base.roles, main },
    defaults: { ...base.defaults, main },
    default_efforts: { main: { effort: 'max', messages: 'high' } },
    available: [...base.available, { alias: 'claude', api: null, effort_supported: true }],
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit = {}) => {
      if (init.method === 'POST') requests.push(init);
      return new Response(JSON.stringify(chosen));
    }),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ModelPicker, { target, props: { projectId: 'p1' } }));
  await settle();
  const trigger = target.querySelector<HTMLButtonElement>('.model-trigger')!;
  expect(trigger.textContent).toContain('Deployment default');
  trigger.click();
  await settle();
  const efforts = [...target.querySelectorAll<HTMLButtonElement>('.effort-group button')];
  const keep = efforts.find((item) => item.textContent === 'Deployment default')!;
  expect(keep.getAttribute('aria-checked')).toBe('true');
  expect(keep.title).toBe('high on the Messages API');
  efforts.find((item) => item.textContent === 'Medium')!.click();
  await settle();
  expect(JSON.parse(String(requests[0]!.body)).roles.main).toEqual({
    alias: 'claude',
    reasoning_effort: 'medium',
    deployment_default: false,
  });
});
