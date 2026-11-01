// SPDX-License-Identifier: MIT
// A panel keeps what it read while its parent polls (owner, 2026-10-21: the Compute tab
// flashed as it refreshed). A prop such as `environmentId={kernelScope.environmentId}`
// reads the parent's object, which each poll replaces with an equal one: only another
// value may restart the panel's own reads.
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ArtifactTable from '../views/ArtifactTable.svelte';
import DocumentView from '../views/DocumentView.svelte';
import KernelPanel from '../views/KernelPanel.svelte';
import PlanPanel from '../views/PlanPanel.svelte';
import type { Pin } from './figureRefs';
import Markdown from './Markdown.svelte';
import { reactiveProps } from './reactiveProps.svelte';

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

function reads(): string[] {
  const urls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      urls.push(url);
      return new Response(JSON.stringify({ error: { kind: 'unavailable', message: 'busy' } }), {
        status: 503,
      });
    }),
  );
  return urls;
}

function show<P extends Record<string, unknown>>(component: Component<P>, props: P): void {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(component, { target, props }));
}

it("keeps the kernel panel's reads across a poll of the session", async () => {
  const urls = reads();
  const polled = reactiveProps({ scope: { environmentId: 'env-1' } });
  show(KernelPanel, {
    projectId: 'p',
    sessionId: 's',
    get environmentId() {
      return polled.scope.environmentId;
    },
  });
  await settle();
  const before = urls.length;
  expect(before).toBeGreaterThan(0);
  polled.scope = { environmentId: 'env-1' };
  await settle();
  expect(urls.length).toBe(before);
});

it("keeps the plan panel's reads across a poll of the session", async () => {
  const urls = reads();
  const polled = reactiveProps({ detail: { closed_at: null as string | null } });
  show(PlanPanel, {
    projectId: 'p',
    sessionId: 's',
    get closed() {
      return polled.detail.closed_at !== null;
    },
  });
  await settle();
  const before = urls.length;
  expect(before).toBeGreaterThan(0);
  polled.detail = { closed_at: null };
  await settle();
  expect(urls.length).toBe(before);
});

it('reads an open document once across polls of the document list', async () => {
  const urls = reads();
  const polled = reactiveProps({ chosen: { document_id: 'doc-1' } });
  show(DocumentView, {
    projectId: 'p',
    get documentId() {
      return polled.chosen.document_id;
    },
    revision: null,
    compact: true,
  });
  await settle();
  const before = urls.length;
  expect(before).toBeGreaterThan(0);
  polled.chosen = { document_id: 'doc-1' };
  await settle();
  expect(urls.length).toBe(before);
});

it('reads an artifact list once across equal filters', async () => {
  const urls = reads();
  const polled = reactiveProps({ filter: { session: 's', run: '', contains: '' } });
  show(ArtifactTable, {
    projectId: 'p',
    group: 'results',
    empty: 'None.',
    get filter() {
      return polled.filter;
    },
  });
  await settle();
  const before = urls.length;
  expect(before).toBeGreaterThan(0);
  polled.filter = { session: 's', run: '', contains: '' };
  await settle();
  expect(urls.length).toBe(before);
  polled.filter = { session: 's', run: '', contains: 'plot' };
  await settle();
  expect(urls.length).toBeGreaterThan(before);
});

it("keeps a text's figures across equal pins", async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            kind: 'image',
            url: 'http://127.0.0.1:8201/file/c.m',
            capability: null,
            expires_at: '2026-10-16T00:10:00Z',
            media_type: 'image/png',
          }),
        ),
    ),
  );
  const pin = (): Pin => ({
    block: 'b2',
    kind: 'image',
    path: 'plots/t.png',
    session: 's1',
    artifact: 'art-1',
    revision: 3,
    sha256: 'ab'.repeat(32),
    size: 100,
    media_type: 'image/png',
  });
  const polled = reactiveProps({ entry: { pins: [pin()] } });
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(Markdown, {
      target,
      props: {
        source: 'Timing:\n\n![Timing](plots/t.png)\n',
        projectId: 'p1',
        get pins() {
          return polled.entry.pins;
        },
      },
    }),
  );
  await settle();
  const drawn = target.querySelector('img');
  expect(drawn).not.toBeNull();
  polled.entry = { pins: [pin()] };
  await settle();
  expect(target.querySelector('img')).toBe(drawn);
});
