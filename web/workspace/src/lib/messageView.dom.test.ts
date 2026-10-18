// SPDX-License-Identifier: MIT
// One archived message, read whole and read-only (CM-3, CM-34): its place, its run and its
// text, shown as text, never as markup.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import MessageView from '../views/MessageView.svelte';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';
const ID = 'msg-0123456789abcdef';

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

describe('an archived message', () => {
  it('shows its place, its run and its whole text, as text', async () => {
    const asked: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        asked.push(url);
        return new Response(
          JSON.stringify({
            message: ID,
            session: 's1',
            position: 41,
            run: 'run-c',
            parent: 'run-a',
            created: '2026-10-16T03:00:00+00:00',
            role: 'tool',
            kind: 'tool_result',
            source: 'sage',
            call_id: 'c1',
            text: `7031\n${HOSTILE}\n`,
          }),
        );
      }),
    );
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(mount(MessageView, { target, props: { projectId: 'p1', messageId: ID } }));
    await settle();
    expect(asked).toEqual([`/api/v0/projects/p1/messages/${ID}`]);
    expect(target.querySelector('pre.message')!.textContent).toBe(`7031\n${HOSTILE}\n`);
    expect(target.querySelector('script, img')).toBeNull();
    const runs = [...target.querySelectorAll('dd a')].map((a) => a.getAttribute('href'));
    expect(runs).toEqual([
      '#/projects/p1/sessions/s1/runs/run-c',
      '#/projects/p1/sessions/s1/runs/run-a',
    ]);
    expect(target.textContent).toContain('41 in its session');
    expect(target.textContent).toContain('tool_result, sage (call c1)');
  });

  it('shows large original text in bounded display pages', async () => {
    const total = 20 * 65536;
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (!url.includes('/chunks'))
        return new Response(
          JSON.stringify({ error: { kind: 'unsupported', message: 'too large' } }),
          { status: 422 },
        );
      const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset') ?? 0);
      const data = (offset < 16 * 65536 ? 'A' : 'B').repeat(65536);
      return new Response(
        JSON.stringify({
          message: ID,
          field: 'content',
          offset,
          total,
          next_offset: offset + 65536 < total ? offset + 65536 : null,
          data_base64: btoa(data),
        }),
      );
    });
    vi.stubGlobal('fetch', fetcher);
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(mount(MessageView, { target, props: { projectId: 'p1', messageId: ID } }));
    await settle();
    expect(target.querySelector('pre.message')!.textContent).toHaveLength(16 * 65536);
    expect(target.querySelector('pre.message')!.textContent).toContain('A');
    target.querySelector<HTMLButtonElement>('button')!.click();
    await settle();
    expect(target.querySelector('pre.message')!.textContent).toHaveLength(4 * 65536);
    expect(target.querySelector('pre.message')!.textContent).not.toContain('A');
    expect(target.querySelector('pre.message')!.textContent).toContain('B');
  });
});
