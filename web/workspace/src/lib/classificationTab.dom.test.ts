// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ClassificationTab from '../views/ClassificationTab.svelte';
import McpServices from '../views/McpServices.svelte';
import type { ClassifiedTool } from './classificationTypes';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.innerHTML = '';
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

const PIN = 'e'.repeat(64);
const tool = (name: string, extra: Partial<ClassifiedTool> = {}): ClassifiedTool => ({
  name,
  effect: 'read',
  reason: `Reads ${name}.`,
  rules: [],
  significant: null,
  sha256: PIN,
  pin_state: 'current',
  mentioned: true,
  escalates: false,
  ...extra,
});

const view = {
  project: 'p1',
  server: 'papers',
  source: {
    kind: 'catalog',
    name: 'papers',
    revision: null,
    pinned: '2026-10-24',
    accepted_at: null,
    accepted_by: null,
    reason: null,
  },
  digest: 'sha256:' + 'd'.repeat(64),
  tools: [
    tool('scrape', {
      rules: [{ argument: 'actions', present: true, equals: null, effect: 'browse' }],
      significant: { url: 'url' },
    }),
    tool('crawl', { pin_state: 'changed' }),
    tool('monitor', {
      effect: 'unclassified',
      pin_state: 'added',
      mentioned: false,
      escalates: true,
      sha256: null,
    }),
    tool('gone', { pin_state: 'removed' }),
    tool('legacy', { pin_state: 'unpinned', sha256: null }),
  ],
  drift: {
    at: '2026-10-25T09:00:00Z',
    pin: 'sha256:' + 'f'.repeat(64),
    added: [{ name: 'monitor', sha256: PIN }],
    removed: ['gone'],
    changed: [{ name: 'crawl', sha256: PIN }],
    held: true,
  },
};

const YAML = `apiVersion: sandbox-science/v0alpha1
kind: ToolClassification
server: papers
tools:
  "monitor":
    class: read
    reason: "Lists monitors."
    sha256: ${PIN}
`;

const drafts = {
  project: 'p1',
  server: 'papers',
  drafts: [
    {
      draft: 'ab12'.repeat(16),
      server: 'papers',
      created_at: '2026-10-25T08:00:00Z',
      model: 'test-model',
      yaml: YAML,
      tools: [
        {
          ...tool('monitor', { reason: 'Lists monitors.' }),
          current_effect: 'unclassified',
          widening: true,
        },
      ],
    },
  ],
};

function stub() {
  const requests: { method: string; url: string; body: unknown }[] = [];
  vi.stubGlobal('innerWidth', 1400);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      const method = options?.method ?? 'GET';
      requests.push({
        method,
        url,
        body: options?.body ? JSON.parse(String(options.body)) : null,
      });
      let answer: unknown = {};
      if (url.endsWith('/accept') && url.includes('/drafts/'))
        answer = { revision: 4, accepted_at: '2026-10-25T10:00:00Z', widenings: ['monitor'] };
      else if (url.endsWith('/drift/accept')) answer = { accepted_at: '2026-10-25T10:05:00Z' };
      else if (url.endsWith('/classification/drafts')) answer = drafts;
      else if (url.endsWith('/classification')) answer = view;
      else if (url.endsWith('/classifications'))
        answer = { project: 'p1', classifications: [view] };
      else if (url.endsWith('/mcp-services'))
        answer = {
          project_id: 'p1',
          policy_revision: 'a',
          inspected_runs: 0,
          skipped_runs: 0,
          observation_scope: 'Bounded',
          services: [
            { name: 'archive', purpose: 'Archive', observation: null },
            { name: 'papers', purpose: 'Papers', observation: null },
          ],
        };
      return new Response(JSON.stringify(answer));
    }),
  );
  return requests;
}

async function tab() {
  const requests = stub();
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ClassificationTab, { target, props: { projectId: 'p1', server: 'papers' } }));
  await settle();
  return { target, requests };
}

const button = (target: HTMLElement, text: string) =>
  [...target.querySelectorAll<HTMLButtonElement>('button')].find(
    (item) => item.textContent?.trim() === text,
  )!;

function type(field: HTMLInputElement | HTMLTextAreaElement, text: string) {
  field.value = text;
  field.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

it('shows the classification in force read-only, with pin states and escalation', async () => {
  const { target, requests } = await tab();
  expect(target.textContent).toContain('calls no tool');
  expect(target.textContent).toContain('checked-in file papers, pinned 2026-10-24');
  const pin = (name: string) =>
    target.querySelector(`.tools tr[data-tool="${name}"] .pin`)?.textContent?.trim();
  expect(['scrape', 'crawl', 'monitor', 'gone', 'legacy'].map(pin)).toEqual([
    'Current',
    'Changed',
    'Added',
    'Removed',
    'Unpinned',
  ]);
  expect(target.querySelector('.tools tr[data-tool="scrape"]')?.textContent).toContain(
    'actions present → browse',
  );
  const unmentioned = target.querySelector('.tools tr[data-tool="monitor"]')!;
  expect(unmentioned.textContent).toContain(
    'Not in the classification: its hints give unclassified',
  );
  expect(unmentioned.querySelector('.escalates')?.textContent).toBe('Escalates');
  expect(target.querySelector('.tools tr[data-tool="crawl"] .escalates')).toBeNull();
  expect(target.querySelector('.drift')?.textContent).toContain(
    "The middleware holds this server's non-read calls",
  );
  expect(requests.every((item) => item.method === 'GET')).toBe(true);
});

it('marks a widening draft and lists the widening in the confirmation', async () => {
  const { target, requests } = await tab();
  const draft = target.querySelector('.classification-draft')!;
  expect(draft.textContent).toContain("model's text");
  const row = draft.querySelector('tr[data-tool="monitor"]')!;
  expect(row.querySelector('.widening')?.textContent).toBe('Widens');
  // An edit that changes a class back to an escalating one is no widening.
  button(row as HTMLElement, 'Edit').click();
  flushSync();
  const select = draft.querySelector<HTMLSelectElement>('.editor select')!;
  select.value = 'write';
  select.dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
  expect(row.querySelector('.widening')).toBeNull();
  select.value = 'read';
  select.dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
  expect(row.querySelector('.widening')?.textContent).toBe('Widens');
  button(target, 'Accept…').click();
  flushSync();
  const confirm = target.querySelector('[role="dialog"]')!;
  expect([...confirm.querySelectorAll('.widenings li')].map((item) => item.textContent)).toEqual([
    'monitor',
  ]);
  expect(confirm.textContent).toContain('unclassified → read');
  button(target, 'Confirm acceptance').click();
  await settle();
  expect(target.textContent).toContain('A reason is required.');
  expect(requests.filter((item) => item.method === 'POST')).toEqual([]);
  type(confirm.querySelector('textarea')!, 'Reviewed the listing');
  button(target, 'Confirm acceptance').click();
  await settle();
  const posts = requests.filter((item) => item.method === 'POST');
  expect(posts.map((item) => item.url)).toEqual([
    `/api/v0/projects/p1/mcp-services/papers/classification/drafts/${'ab12'.repeat(16)}/accept`,
  ]);
  const body = posts[0]!.body as Record<string, string>;
  expect(body.reason).toBe('Reviewed the listing');
  expect(body.yaml).toBe(YAML);
  expect(body.operation_id).toMatch(/^[0-9a-f-]{36}$/);
  expect(target.textContent).toContain('Accepted as revision 4');
  expect(target.textContent).toContain('widenings: monitor');
  // The view is read again after the acceptance.
  expect(requests.filter((item) => item.url.endsWith('/classification'))).toHaveLength(2);
});

it('re-accepts drift only with a reason', async () => {
  const { target, requests } = await tab();
  button(target, 'Re-accept').click();
  await settle();
  expect(target.textContent).toContain('A reason is required to re-accept.');
  expect(requests.filter((item) => item.method === 'POST')).toEqual([]);
  type(target.querySelector<HTMLTextAreaElement>('.drift textarea')!, 'Checked the new tool');
  button(target, 'Re-accept').click();
  await settle();
  const posts = requests.filter((item) => item.method === 'POST');
  expect(posts.map((item) => item.url)).toEqual([
    '/api/v0/projects/p1/mcp-services/papers/drift/accept',
  ]);
  expect((posts[0]!.body as Record<string, string>).reason).toBe('Checked the new tool');
  expect(target.textContent).toContain('Re-accepted at');
});

it("opens a service's Classification tab from the project's list", async () => {
  const requests = stub();
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(McpServices, { target, props: { projectId: 'p1' } }));
  await settle();
  const row = target.querySelector<HTMLButtonElement>('.classification-row')!;
  expect(row.textContent).toContain('papers');
  expect(row.textContent).toContain('pinned 2026-10-24');
  expect(row.textContent).toContain('5 tools');
  expect(row.textContent).toContain('Drift · calls held');
  row.click();
  await settle();
  expect(target.querySelector('.mcp-detail h2')?.textContent).toBe('papers');
  expect(target.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe(
    'Classification',
  );
  expect(target.querySelector('.tools tr[data-tool="monitor"]')).not.toBeNull();
  expect(requests.every((item) => item.method === 'GET')).toBe(true);
});

it('sends the edited draft as YAML, with the rule changes confirmed', async () => {
  const { target, requests } = await tab();
  const draft = target.querySelector<HTMLElement>('.classification-draft')!;
  button(draft.querySelector<HTMLElement>('tr[data-tool="monitor"]')!, 'Edit').click();
  flushSync();
  const editor = draft.querySelector<HTMLElement>('.editor')!;
  const [effect, condition, ruleEffect] = [...editor.querySelectorAll<HTMLSelectElement>('select')];
  effect!.value = 'write';
  effect!.dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
  const [reason, argument] = [...editor.querySelectorAll<HTMLInputElement>('input')];
  type(reason!, 'Lists and creates monitors');
  type(argument!, 'dry_run');
  condition!.value = 'equals';
  condition!.dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
  type(editor.querySelectorAll<HTMLInputElement>('input')[2]!, 'true');
  ruleEffect!.value = 'read';
  ruleEffect!.dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
  button(editor, 'Add rule').click();
  flushSync();
  expect(draft.querySelector('tr[data-tool="monitor"] .widening')).toBeNull();
  button(target, 'Accept…').click();
  flushSync();
  const confirm = target.querySelector<HTMLElement>('[role="dialog"]')!;
  expect(confirm.textContent).toContain('+ dry_run = true → read');
  expect(confirm.textContent).toContain('unclassified → write');
  type(confirm.querySelector('textarea')!, 'Edited after review');
  button(target, 'Confirm acceptance').click();
  await settle();
  const body = requests.find((item) => item.method === 'POST')!.body as Record<string, string>;
  expect(body.yaml).toBe(`apiVersion: sandbox-science/v0alpha1
kind: ToolClassification
server: papers
tools:
  "monitor":
    class: write
    reason: "Lists and creates monitors"
    sha256: ${PIN}
    rules:
      - {argument: dry_run, equals: true, class: read}
`);
});
