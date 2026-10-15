// SPDX-License-Identifier: MIT
// @vitest-environment-options {"settings": {"disableIframePageLoading": true}}
// The PDF view opens the viewer at the page its address names, such as a quote's. The
// frame loads nothing here.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import PdfView from '../views/PdfView.svelte';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

const URL = 'http://127.0.0.2:8201/viewer/#c=abc.def&w=http%3A%2F%2F127.0.0.1%3A8200';

async function frame(page: number | null): Promise<string | null> {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({ url: URL, expires_at: '2026-10-15T00:10:00Z', path: 'paper.pdf' }),
        ),
    ),
  );
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(PdfView, {
      target,
      props: { projectId: 'p1', artifactId: 'art-1', revision: 2, sha256: 'ab'.repeat(32), page },
    }),
  );
  for (let i = 0; i < 6; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
  return target.querySelector('iframe')?.getAttribute('src') ?? null;
}

describe('the PDF view', () => {
  it('opens the viewer at the page named, in the fragment', async () => {
    expect(await frame(14)).toBe(`${URL}&p=14`);
  });

  it('opens the viewer at its first page when none is named', async () => {
    expect(await frame(null)).toBe(URL);
  });
});
