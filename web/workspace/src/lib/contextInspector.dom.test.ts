// SPDX-License-Identifier: MIT
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import type { RunRecord } from './api';
import { contextUsage } from './contextUsage';
import ContextInspector from '../views/ContextInspector.svelte';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(async () => {
  for (const component of mounted.splice(0)) await unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle() {
  for (let index = 0; index < 6; index++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

it('keeps missing context calls as graph gaps and weights known cache hits by input', () => {
  const found = contextUsage([
    { model: 'm', input_tokens: 100, output_tokens: 1, cache_read_input_tokens: 50 },
    { model: 'm', input_tokens: null, output_tokens: 1 },
    { model: 'm', input_tokens: 200, output_tokens: 1 },
    {
      model: 'm',
      input_tokens: 300,
      output_tokens: 1,
      cache_read_input_tokens: 150,
      cache_creation_input_tokens: 80,
    },
  ]);
  expect(found.segments.map((segment) => segment.map((point) => point.call))).toEqual([
    [1],
    [3, 4],
  ]);
  expect(found.eligible).toBe(3);
  expect(found.cacheKnown).toBe(2);
  expect(found.readShare).toBe(0.5);
  expect(found.cacheWrite).toBe(80);
});

it('opens the saved attempt history and submits server-side search', async () => {
  const runName = '20261018T120000000000Z-run-1';
  const run: RunRecord = {
    run_id: 'run-1',
    role: 'delegated',
    context: {
      project_id: 'p1',
      session_id: 's1',
      environment_id: 'e1',
      policy_revision: null,
      actor: 'agent',
    },
    task: 'Investigate',
    started_at: '2026-10-18T12:00:00Z',
    ended_at: '2026-10-18T12:05:00Z',
    stopped: 'answered',
    final_text: 'Done',
    messages: [],
    tools: [],
    usage: [
      {
        model: 'm',
        input_tokens: 100,
        output_tokens: 10,
        cache_read_input_tokens: 25,
      },
    ],
    summaries: {},
  };
  const fetcher = vi.fn(
    async (_input: RequestInfo | URL) =>
      new Response(
        JSON.stringify({
          messages: [
            {
              message: 'msg-1234567890abcdef',
              position: 1,
              run: runName,
              parent: null,
              created: '2026-10-18T12:00:00Z',
              role: 'user',
              kind: 'task',
              source: null,
              snippet: 'SNOVA target',
            },
          ],
          next_after: null,
          coverage: 'Retained original text',
        }),
      ),
  );
  vi.stubGlobal('fetch', fetcher);
  const target = document.createElement('div');
  document.body.append(target);
  const close = vi.fn();
  mounted.push(mount(ContextInspector, { target, props: { run, runName, close } }));
  await settle();
  expect(target.textContent).toContain('Cache-read share: 25.0%');
  const history = [...target.querySelectorAll<HTMLButtonElement>('.context-tabs button')].find(
    (button) => button.textContent === 'Original history',
  )!;
  history.click();
  await settle();
  expect(target.textContent).toContain('SNOVA target');
  const search = target.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.value = 'SNOVA';
  search.dispatchEvent(new Event('input', { bubbles: true }));
  target
    .querySelector('form')!
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(String(fetcher.mock.calls[1]?.[0])).toContain('contains=SNOVA');
  target.querySelector<HTMLButtonElement>('[aria-label="Close context management"]')!.click();
  expect(close).toHaveBeenCalledOnce();
});

it('loads archived usage and fold summaries instead of treating a compact run as empty', async () => {
  const runName = '20261018T120000000000Z-run-1';
  const run: RunRecord = {
    run_id: 'run-1',
    context: {
      project_id: 'p1',
      session_id: 's1',
      environment_id: 'e1',
      policy_revision: null,
      actor: 'agent',
    },
    task: 'Research',
    started_at: '2026-10-18T12:00:00Z',
    ended_at: '2026-10-18T12:05:00Z',
    stopped: 'answered',
    final_text: null,
    messages: [],
    tools: [],
    usage: [],
    stored_history: {
      format: 'run-history-v1',
      project: 'p1',
      session: 's1',
      run: runName,
      collections: { usage: 2, summaries: 1 },
    },
    summaries: { folds: { folds: { stored: 'summaries/folds/folds', items: 1 } } },
  };
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('collection=usage'))
      return new Response(
        JSON.stringify({
          collection: 'usage',
          offset: 0,
          next_offset: null,
          total: 2,
          items: [
            {
              position: 0,
              value: {
                model: 'm',
                input_tokens: 100,
                output_tokens: 10,
                cache_read_input_tokens: 25,
              },
            },
            {
              position: 1,
              value: {
                model: 'm',
                input_tokens: 200,
                output_tokens: 20,
                cache_read_input_tokens: 100,
              },
            },
          ],
        }),
      );
    return new Response(
      JSON.stringify({
        collection: 'summaries',
        offset: 0,
        next_offset: null,
        total: 1,
        items: [
          {
            position: 0,
            value: {
              source: 'folds',
              value: {
                folds: [{ fold: 'msg-1234567890abcdef', level: 'fold' }],
                rebuilds: [],
              },
            },
          },
        ],
      }),
    );
  });
  vi.stubGlobal('fetch', fetcher);
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ContextInspector, { target, props: { run, runName, close: vi.fn() } }));
  await settle();
  expect(target.textContent).toContain('Cache-read share: 41.7%');
  expect(target.textContent).not.toContain('No provider input counts');
  const folds = [...target.querySelectorAll<HTMLButtonElement>('.context-tabs button')].find(
    (button) => button.textContent === 'Folds',
  )!;
  folds.click();
  await settle();
  expect(target.textContent).toContain('Open fold record');
  expect(target.textContent).not.toContain('No fold summary is saved');
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it('reads an archived fold summary over the collection listing limit in byte pages', async () => {
  const runName = '20261018T120000000000Z-run-1';
  const run: RunRecord = {
    run_id: 'run-1',
    context: {
      project_id: 'p1',
      session_id: 's1',
      environment_id: 'e1',
      policy_revision: null,
      actor: 'agent',
    },
    task: 'Research',
    started_at: '2026-10-18T12:00:00Z',
    ended_at: '2026-10-18T12:05:00Z',
    stopped: 'answered',
    final_text: null,
    messages: [],
    tools: [],
    usage: [],
    summaries: {},
    stored_history: {
      format: 'run-history-v1',
      project: 'p1',
      session: 's1',
      run: runName,
      collections: { summaries: 1 },
    },
  };
  const saved = JSON.stringify({
    source: 'folds',
    value: {
      folds: [{ fold: 'msg-1234567890abcdef', level: 'fold' }],
      filler: 'x'.repeat(70000),
    },
  });
  const raw = new TextEncoder().encode(saved);
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (!url.includes('/chunks'))
      return new Response(
        JSON.stringify({
          error: { kind: 'unsupported', message: 'collection item requires byte paging' },
        }),
        { status: 422 },
      );
    const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset') ?? 0);
    const piece = raw.slice(offset, offset + 65536);
    const binary = String.fromCharCode(...piece);
    return new Response(
      JSON.stringify({
        collection: 'summaries',
        offset,
        total: raw.length,
        next_offset: offset + piece.length < raw.length ? offset + piece.length : null,
        data_base64: btoa(binary),
      }),
    );
  });
  vi.stubGlobal('fetch', fetcher);
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ContextInspector, { target, props: { run, runName, close: vi.fn() } }));
  [...target.querySelectorAll<HTMLButtonElement>('.context-tabs button')]
    .find((button) => button.textContent === 'Folds')!
    .click();
  await settle();
  expect(target.textContent).toContain('Open fold record');
  expect(fetcher).toHaveBeenCalledTimes(3);
});

it('keeps paging on submitted filters and clears stale results when a new search fails', async () => {
  const runName = '20261018T120000000000Z-run-1';
  const run: RunRecord = {
    run_id: 'run-1',
    context: {
      project_id: 'p1',
      session_id: 's1',
      environment_id: 'e1',
      policy_revision: null,
      actor: 'agent',
    },
    task: 'Research',
    started_at: '2026-10-18T12:00:00Z',
    ended_at: '2026-10-18T12:05:00Z',
    stopped: 'answered',
    final_text: null,
    messages: [],
    tools: [],
    usage: [],
  };
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).includes('contains=new'))
      return new Response(
        JSON.stringify({ error: { kind: 'unavailable', message: 'Search failed' } }),
        { status: 500 },
      );
    return new Response(
      JSON.stringify({
        messages: [
          {
            message: `msg-${fetcher.mock.calls.length.toString(16).padStart(16, '0')}`,
            position: fetcher.mock.calls.length,
            run: runName,
            parent: null,
            created: '2026-10-18T12:00:00Z',
            role: 'user',
            kind: 'task',
            source: null,
            snippet: 'old result',
          },
        ],
        next_after: fetcher.mock.calls.length === 1 ? 1 : null,
        coverage: 'partial',
      }),
    );
  });
  vi.stubGlobal('fetch', fetcher);
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(ContextInspector, { target, props: { run, runName, close: vi.fn() } }));
  const history = [...target.querySelectorAll<HTMLButtonElement>('.context-tabs button')].find(
    (button) => button.textContent === 'Original history',
  )!;
  history.click();
  await settle();
  const search = target.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.value = 'new';
  search.dispatchEvent(new Event('input', { bubbles: true }));
  target.querySelector<HTMLButtonElement>('.history-results + button')!.click();
  await settle();
  expect(String(fetcher.mock.calls[1]?.[0])).not.toContain('contains=new');
  target
    .querySelector('form')!
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  expect(String(fetcher.mock.calls[2]?.[0])).toContain('contains=new');
  expect(target.textContent).not.toContain('old result');
  expect(target.textContent).toContain('Search failed');
});
