// SPDX-License-Identifier: MIT
// Figure images (FG-12, FG-13): a PNG or a JPEG shows from the preview origin, for the
// capability the workspace mints when it is shown; any other type, or an image the
// server or the preview origin refuses, is its path with the reason, never a blank.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ArtifactRow from '../views/ArtifactRow.svelte';
import FigureImage from '../views/FigureImage.svelte';
import type { ArtifactEntry } from './api-types';
import { imageSource, MAX_IMAGE_BYTES, shownAsImage } from './figures';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';
const SHA = 'ab'.repeat(32);

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

const entry = (fields: Partial<ArtifactEntry> = {}): ArtifactEntry => ({
  artifact_id: 'art-1',
  revision: 3,
  path: 'plots/timing.png',
  sha256: SHA,
  size: 1000,
  media_type: 'image/png',
  created_at: '2026-10-16T00:00:00Z',
  result: true,
  deleted: false,
  content_retained: true,
  content_note: null,
  kind: 'agent_publication',
  session_id: 's1',
  run_id: 'run-1',
  role: 'main',
  tool_call: 'c1',
  ...fields,
});

function show(props: { alt: string }) {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(FigureImage, {
      target,
      props: { projectId: 'p1', artifact: 'art-1', revision: 3, sha256: SHA, ...props },
    }),
  );
  return target;
}

describe('which files show as images', () => {
  it('is a kept PNG or JPEG within the size, with the preview origin on', () => {
    expect(shownAsImage(entry(), true)).toBe(true);
    expect(shownAsImage(entry({ media_type: 'image/jpeg' }), true)).toBe(true);
    expect(shownAsImage(entry(), false)).toBe(false);
    expect(shownAsImage(entry({ media_type: 'image/svg+xml' }), true)).toBe(false);
    expect(shownAsImage(entry({ media_type: 'text/html' }), true)).toBe(false);
    expect(shownAsImage(entry({ size: MAX_IMAGE_BYTES + 1 }), true)).toBe(false);
    expect(shownAsImage(entry({ content_retained: false }), true)).toBe(false);
    expect(imageSource('javascript:alert(1)')).toBeNull();
    expect(imageSource('data:image/png;base64,AA')).toBeNull();
    expect(imageSource('http://127.0.0.1:8766/file/x.y')).toBe('http://127.0.0.1:8766/file/x.y');
  });

  it('offers a PNG in the artifact list, and an SVG only as its path', () => {
    const row = (found: ArtifactEntry) => {
      const table = document.createElement('tbody');
      document.body.append(table);
      mounted.push(
        mount(ArtifactRow, {
          target: table,
          props: { projectId: 'p1', entry: found, viewer: true },
        }),
      );
      return table;
    };
    expect(row(entry()).textContent).toContain('Show the image');
    const svg = row(entry({ media_type: 'image/svg+xml', path: HOSTILE }));
    expect(svg.textContent).not.toContain('Show the image');
    expect(svg.textContent).toContain(HOSTILE);
    expect(svg.querySelector('script, img')).toBeNull();
  });
});

describe('an image from the preview origin', () => {
  it('loads the capability minted for it, with no referrer', async () => {
    const asked: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        asked.push(url);
        return new Response(
          JSON.stringify({
            url: 'http://127.0.0.1:8766/file/cap.mac',
            expires_at: '2026-10-16T00:10:00Z',
            media_type: 'image/png',
          }),
        );
      }),
    );
    const target = show({ alt: 'Timing by key size' });
    await settle();
    expect(asked).toEqual([`/api/v0/projects/p1/artifacts/art-1/revisions/3/figure?sha256=${SHA}`]);
    const image = target.querySelector('img')!;
    expect(image.getAttribute('src')).toBe('http://127.0.0.1:8766/file/cap.mac');
    expect(image.getAttribute('referrerpolicy')).toBe('no-referrer');
    expect(image.getAttribute('alt')).toBe('Timing by key size');
    // The preview origin refuses it (its bytes are not a PNG): the path and the reason.
    image.dispatchEvent(new Event('error'));
    flushSync();
    expect(target.querySelector('img')).toBeNull();
    expect(target.textContent).toContain('Not shown: Timing by key size');
  });

  it('shows the path and the reason, as text, when the server will not give it', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              error: { kind: 'unsupported', message: 'this image is larger than a page shows' },
            }),
            { status: 422, headers: { 'Content-Type': 'application/json' } },
          ),
      ),
    );
    const target = show({ alt: HOSTILE });
    await settle();
    expect(target.querySelector('img, script')).toBeNull();
    expect(target.textContent).toContain(HOSTILE);
    expect(target.textContent).toContain('larger than a page shows');
  });

  it('loads nothing the server names on another scheme', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              url: 'javascript:alert(1)',
              expires_at: '2026-10-16T00:10:00Z',
              media_type: 'image/png',
            }),
          ),
      ),
    );
    const target = show({ alt: 'x' });
    await settle();
    expect(target.querySelector('img')).toBeNull();
    expect(target.textContent).toContain('no usable address');
  });
});
