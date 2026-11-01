// SPDX-License-Identifier: MIT
// A text file opened in the Artifacts page: line numbers, its code highlighted, and its
// JSON laid out for reading, with the file as saved one click away.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ArtifactViewer from '../views/ArtifactViewer.svelte';
import type { ArtifactEntry } from './api-types';

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

function open(path: string, text: string) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) =>
      url.includes('/content?')
        ? new Response(text)
        : new Response(JSON.stringify({ revisions: [], next_before: null })),
    ),
  );
  const entry = {
    artifact_id: 'art-1',
    revision: 2,
    path,
    sha256: 'ab'.repeat(32),
    size: text.length,
    media_type: 'text/plain',
    created_at: '2026-10-21T18:00:00Z',
    result: true,
    deleted: false,
    content_retained: true,
    content_note: null,
    kind: 'file',
    session_id: 'main',
    run_id: 'run-1',
    role: 'main',
    tool_call: 'call-1',
  } satisfies ArtifactEntry;
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(ArtifactViewer, {
      target,
      props: { projectId: 'p1', entry, viewer: false, close: () => {}, choose: () => {} },
    }),
  );
  return target;
}

it('lays JSON out for reading, numbers as written, and shows it as saved on request', async () => {
  const saved = '{"rank":80,"digest":123456789012345678901234567890,"ok":true}';
  const target = open('sessions/main/full40.json', saved);
  await settle();
  const code = () => target.querySelector('.file-text')!.textContent;
  expect(code()).toBe(
    '{\n  "rank": 80,\n  "digest": 123456789012345678901234567890,\n  "ok": true\n}\n',
  );
  expect(target.querySelector('.gutter')!.textContent).toBe('1\n2\n3\n4\n5');
  expect([...target.querySelectorAll('.tok-key')].map((key) => key.textContent)).toEqual([
    '"rank"',
    '"digest"',
    '"ok"',
  ]);
  const toggle = [...target.querySelectorAll('button')].find((b) => b.textContent === 'As saved')!;
  toggle.click();
  flushSync();
  expect(code()).toBe(saved);
  expect(toggle.textContent).toBe('Format');
});

it('highlights a script, and offers no layout for what is not JSON', async () => {
  const script = '# check\nH=matrix(F,512,512)\ns="<img src=x onerror=alert(1)><script>"\n';
  const target = open('sessions/main/run.sage', script);
  await settle();
  expect(target.querySelector('.tok-comment')!.textContent).toBe('# check');
  expect(target.querySelector('.tok-builtin')!.textContent).toBe('matrix');
  expect(target.querySelector('.file-text')!.textContent).toBe(script);
  expect([...target.querySelectorAll('button')].some((b) => b.textContent === 'As saved')).toBe(
    false,
  );
  // The file's text is drawn as text: nothing in it becomes markup.
  expect(target.querySelector('.file-text script, .file-text img')).toBeNull();
});
