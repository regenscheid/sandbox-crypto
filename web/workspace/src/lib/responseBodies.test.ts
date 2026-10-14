// SPDX-License-Identifier: MIT
import { afterEach, expect, it, vi } from 'vitest';
import { render } from 'svelte/server';
import ResponseBody from '../views/ResponseBody.svelte';
import { reviews, type ResponseBody as Body } from './reviews';

afterEach(() => vi.unstubAllGlobals());
it('shows original versus corrected MAIN source independently of scientific history', () => {
  const source: Body = {
    source: 'response:exact:explanation',
    finding: 'finding-exact',
    response_revision: 3,
    revision: 2,
    body: 'Corrected $x$ <script>untrusted</script>',
    sha256: 'a'.repeat(64),
    original_author: { kind: 'agent', run_id: 'original-main' },
    author: { kind: 'agent', run_id: 'correcting-main' },
    held: null,
  };
  const html = render(ResponseBody, { props: { projectId: 'p', sessionId: 's', source } }).body;
  expect(html).toContain('original decision revision 3');
  expect(html).toContain('body revision 2');
  expect(html).toContain('original-main');
  expect(html).toContain('correcting-main');
  expect(html).toContain('Read previous explanation body');
  expect(html).toContain('Source corrections preserve the original decision');
  expect(html).not.toContain('<script>');
  expect(html).not.toContain('Save');
  expect(html).not.toContain('<textarea');
});
it('uses a scoped read-only original-response/body-revision route', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ revision: 1 })));
  vi.stubGlobal('fetch', fetch);
  await reviews.responseBody('p/one', 's/two', 'finding/exact', 3, 1);
  expect(fetch.mock.calls[0]?.[0]).toBe(
    '/api/v0/projects/p%2Fone/sessions/s%2Ftwo/findings/finding%2Fexact/responses/3?body_revision=1',
  );
  expect(fetch.mock.calls[0]?.[1]?.method ?? 'GET').toBe('GET');
});
