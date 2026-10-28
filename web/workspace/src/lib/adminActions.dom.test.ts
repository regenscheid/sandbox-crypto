// SPDX-License-Identifier: MIT
// Login through Authentik (D104): the sidebar names the session's user, and the admin-only
// actions stay on the page, disabled with the reason, for a user who is not an admin.
// Every other answer stays the user's: allow once or for the session, stop, turn off.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ChatHold from '../views/ChatHold.svelte';
import ChatPermission from '../views/ChatPermission.svelte';
import McpServices from '../views/McpServices.svelte';
import WorkspaceSidebar from '../views/WorkspaceSidebar.svelte';
import type { PendingApproval, Viewer } from './api-types';
import { adminBlock, setViewer } from './viewer.svelte';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.innerHTML = '';
  sessionStorage.clear();
  vi.unstubAllGlobals();
  setViewer(null);
});

async function settle() {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

const MEMBER: Viewer = { user: 'bob', login: 'forward', admin: false, admin_group: 'atlas-admins' };
const ADMIN: Viewer = { ...MEMBER, user: 'carol', admin: true };
const OPERATOR: Viewer = { user: 'user', login: 'token', admin: true, admin_group: null };
const REASON = 'Only members of the Authentik group atlas-admins can do this.';

function place(component: any, props: Record<string, unknown>): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(component, { target, props }));
  flushSync();
  return target;
}

const button = (target: HTMLElement, text: string) =>
  [...target.querySelectorAll<HTMLButtonElement>('button')].find(
    (item) => item.textContent?.trim() === text,
  )!;

const base = {
  seq: 7,
  source: 'worker',
  run: null,
  server: '',
  tool: '',
  method: '',
  policy: 'sha256:' + 'a'.repeat(64),
  session: 'main',
  rule: null,
  reason: null,
  recorded_at: '2026-10-28T12:00:00Z',
  expires_at: '2026-10-28T12:30:00Z',
  timed_out: false,
  on_timeout: 'deny',
  child: null,
  caller_kind: 'agent',
  effect: null,
  max_grant: null,
  summary: {},
  http: null,
  packages: null,
  hold: null,
  scopes: [],
} satisfies Omit<PendingApproval, 'kind'>;

const call: PendingApproval = {
  ...base,
  kind: 'mcp',
  server: 'crypto-research-mcp',
  tool: 'web_fetch',
  method: 'tools/call',
  effect: 'write',
  max_grant: 'project',
  scopes: ['once', 'session', 'project'],
};

const hold: PendingApproval = {
  ...base,
  kind: 'hold',
  caller_kind: 'code',
  hold: {
    rule: 'host-rate',
    count: 301,
    threshold: 300,
    window_seconds: 60,
    host: 'example.org',
    requests: [],
  },
};

describe('the session’s user', () => {
  it('is named in the sidebar, and a login link is the operator', async () => {
    vi.stubGlobal('innerWidth', 1400);
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ projects: [] }))),
    );
    const target = place(WorkspaceSidebar, {
      route: { view: 'projects' },
      close: () => {},
      logout: () => {},
    });
    expect(target.querySelector('.signed-in')).toBeNull(); // not known yet
    setViewer(MEMBER);
    flushSync();
    const said = () => target.querySelector('.signed-in')?.textContent?.replace(/\s+/g, ' ');
    expect(said()).toContain('Signed in as bob');
    expect(said()).toContain('(not an admin)');
    setViewer(OPERATOR);
    flushSync();
    expect(said()).toContain('Signed in as operator (login link)');
    expect(said()).not.toContain('not an admin');
  });

  it('blocks nothing before the server says who it is, or for an admin', () => {
    expect(adminBlock()).toBeNull();
    setViewer(ADMIN);
    expect(adminBlock()).toBeNull();
    setViewer(MEMBER);
    expect(adminBlock()).toBe(REASON);
    setViewer({ ...MEMBER, admin_group: null });
    expect(adminBlock()).toContain('admins');
  });
});

describe('a permission card', () => {
  it('disables the project grant for a member, with the reason', async () => {
    setViewer(MEMBER);
    const sent: string[] = [];
    const target = place(ChatPermission, {
      request: call,
      answer: async (decision: string) => {
        sent.push(decision);
      },
    });
    target.querySelector<HTMLButtonElement>('.caret')!.click();
    flushSync();
    const items = [...target.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')];
    const project = items.find((item) => item.textContent?.includes('This project'))!;
    expect(project.disabled).toBe(true);
    expect(project.title).toBe(REASON);
    expect(project.textContent).toContain(REASON);
    const session = items.find((item) => item.textContent?.includes('This session'))!;
    expect(session.disabled).toBe(false);
    session.click();
    flushSync();
    target.querySelector<HTMLButtonElement>('.allow')!.click();
    await settle();
    expect(sent).toEqual(['session']);
  });

  it('offers the project grant to an admin', () => {
    setViewer(ADMIN);
    const target = place(ChatPermission, { request: call, answer: async () => {} });
    target.querySelector<HTMLButtonElement>('.caret')!.click();
    flushSync();
    const project = [...target.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')].find(
      (item) => item.textContent?.includes('This project'),
    )!;
    expect(project.disabled).toBe(false);
    expect(target.textContent).not.toContain(REASON);
  });
});

describe('a hold', () => {
  it('lets a member stop the session but not clear the hold', async () => {
    setViewer(MEMBER);
    const sent: string[] = [];
    const target = place(ChatHold, {
      request: hold,
      answer: async (decision: string) => {
        sent.push(decision);
      },
    });
    const clear = button(target, 'Clear hold');
    expect(clear.disabled).toBe(true);
    expect(clear.title).toBe(REASON);
    expect(target.querySelector('.admin-only')?.textContent).toContain(REASON);
    button(target, 'Stop session').click();
    await settle();
    expect(sent).toEqual(['stop']);
  });

  it('lets an admin clear it', () => {
    setViewer(ADMIN);
    const target = place(ChatHold, { request: hold, answer: async () => {} });
    expect(button(target, 'Clear hold').disabled).toBe(false);
    expect(target.querySelector('.admin-only')).toBeNull();
  });
});

describe('an MCP service turned off', () => {
  function stub() {
    vi.stubGlobal('innerWidth', 1400);
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.endsWith('/mcp-services/settings'))
          return new Response(
            JSON.stringify({
              project: 'p1',
              revision: 1,
              configured: ['papers', 'web'],
              disabled: ['papers'],
            }),
          );
        if (url.endsWith('/mcp-services'))
          return new Response(
            JSON.stringify({
              project_id: 'p1',
              policy_revision: 'a',
              inspected_runs: 0,
              skipped_runs: 0,
              observation_scope: 'Bounded',
              services: [
                { name: 'papers', purpose: 'Papers', observation: null },
                { name: 'web', purpose: 'Web', observation: null },
              ],
            }),
          );
        return new Response(JSON.stringify({}));
      }),
    );
  }

  it('stays off for a member, who may still turn another one off', async () => {
    setViewer(MEMBER);
    stub();
    const target = place(McpServices, { projectId: 'p1' });
    await settle();
    target.querySelectorAll<HTMLButtonElement>('.mcp-row')[0]!.click();
    flushSync();
    const on = button(target, 'Turn on');
    expect(on.disabled).toBe(true);
    expect(on.title).toBe(REASON);
    expect(target.querySelector('.admin-only')?.textContent).toContain('widens');
    target.querySelectorAll<HTMLButtonElement>('.mcp-row')[1]!.click();
    flushSync();
    expect(button(target, 'Turn off').disabled).toBe(false);
  });

  it('can be turned on by an admin', async () => {
    setViewer(ADMIN);
    stub();
    const target = place(McpServices, { projectId: 'p1' });
    await settle();
    target.querySelectorAll<HTMLButtonElement>('.mcp-row')[0]!.click();
    flushSync();
    expect(button(target, 'Turn on').disabled).toBe(false);
  });
});
