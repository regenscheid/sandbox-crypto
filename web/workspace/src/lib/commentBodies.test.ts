// SPDX-License-Identifier: MIT
import { afterEach, expect, it, vi } from 'vitest';
// Rendered HTML is compared with each whitespace run as one space, so that line breaks in
// the components' source do not matter.
import { render } from 'svelte/server';
import CommentBodyEditor from '../views/CommentBodyEditor.svelte';
import AnnotationThread from '../views/AnnotationThread.svelte';
import { type CommentBodySource, type CommentThread, api } from './api';
import { saveDraft, clearDraft } from './drafts';
import { parser } from './markdown';
import { NO_MACROS } from './macros';

afterEach(() => vi.unstubAllGlobals());
const source: CommentBodySource = {
  entry_id: 'cmt-original',
  revision: 3,
  sha256: 'a'.repeat(64),
  body: 'Current $x$.',
  original_author: { kind: 'user', user: 'user' },
  author: { kind: 'user', user: 'user' },
};

it('retains a separate original-body draft with stale thread and exact body CAS', () => {
  const key = 'doc:comment-body:cmt-original';
  saveDraft('p', key, {
    text: 'Unsaved <script> body',
    base: 4,
    reason: 'edit_body',
    retained: JSON.stringify({ base: 2, sha256: 'b'.repeat(64) }),
    block: null,
  });
  saveDraft('p', 'doc:reply:thread', {
    text: 'Independent reply draft',
    base: 5,
    reason: 'reply',
    retained: '',
    block: null,
  });
  const html = render(CommentBodyEditor, {
    props: {
      projectId: 'p',
      documentId: 'doc',
      threadId: 'thread',
      expected: 6,
      source,
      changed: async () => {},
    },
  }).body.replace(/\s+/g, ' ');
  expect(html).toContain('body revision 2, thread revision 4');
  expect(html).toContain('Use current body and thread revisions');
  expect(html).toContain('Unsaved &lt;script> body');
  expect(html).not.toContain('<script>');
  expect(html).not.toContain('Independent reply draft');
  expect(html).toContain('History and existing work requests retain their original text');
  clearDraft('p', key);
  clearDraft('p', 'doc:reply:thread');
});

it('renders immutable original and correcting actor while exposing USER-own body editing only', () => {
  const thread: CommentThread = {
    thread_id: 'ann-test',
    revision: 3,
    status: 'open',
    entries_before: null,
    created_at: 1,
    opener: { kind: 'user', user: 'user' },
    requests: [],
    view_revision: 1,
    anchor: {
      revision: 1,
      block: 'b1',
      math: null,
      start: null,
      end: null,
      kind: 'block',
      quote: 'Context',
      quote_truncated: false,
      before: '',
      after: '',
    },
    attachment: { status: 'current', quote: 'Context', current: 'Context' },
    entries: [
      {
        entry_id: 'cmt-original',
        number: 1,
        kind: 'create',
        author: { kind: 'user', user: 'user' },
        body: 'Original body',
        payload: {},
        created_at: 1,
        body_source: source,
      },
      {
        entry_id: 'cmt-agent',
        number: 2,
        kind: 'reply',
        author: { kind: 'agent', session_id: 's', run_id: 'delegate', role: 'delegated' },
        body: 'Delegated original',
        payload: {},
        created_at: 2,
        body_source: {
          ...source,
          entry_id: 'cmt-agent',
          author: { kind: 'agent', session_id: 's', run_id: 'main', role: 'main' },
        },
      },
      {
        entry_id: 'cmt-correction',
        number: 3,
        kind: 'edit_body',
        author: { kind: 'agent', session_id: 's', run_id: 'main', role: 'main' },
        body: 'Corrected reply',
        payload: { entry: 'cmt-agent', body_revision: 3 },
        created_at: 3,
      },
    ],
  };
  const html = render(AnnotationThread, {
    props: {
      projectId: 'p',
      documentId: 'doc',
      thread,
      sessions: [],
      macros: NO_MACROS,
      changed: async () => {},
    },
  }).body.replace(/\s+/g, ' ');
  expect(html).toContain('Original body');
  expect(html).toContain('Delegated original');
  expect(html).toContain('Correction of original entry');
  expect(html).toContain('Current body revision 3');
  expect(html.match(/Edit this comment body/g)).toHaveLength(1);
});

it('submits corrections through the existing authenticated same-origin route with explicit source CAS', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ revision: 7 })));
  vi.stubGlobal('fetch', fetch);
  await api.changeComment('p', 'doc', {
    action: 'edit_body',
    thread: 'ann',
    entry: 'cmt-original',
    expected: 6,
    base: 3,
    sha256: 'a'.repeat(64),
    body: '',
    operation_id: 'durable-retry',
  });
  expect(fetch.mock.calls[0]?.[0]).toBe('/api/v0/projects/p/documents/doc/comments');
  expect(JSON.parse(fetch.mock.calls[0]?.[1]?.body)).toMatchObject({
    entry: 'cmt-original',
    base: 3,
    body: '',
    operation_id: 'durable-retry',
  });
});

it.each(['---\nformula: $\\unknown{x}$\n---\n', '::: lemma\n$\\unknown{x}$\n:::\n'])(
  'ordinary comment Markdown exposes the math in document-looking text: %s',
  (text) => {
    const spans: string[] = [];
    for (const token of parser.parse(text, {})) {
      if (token.type.startsWith('math_')) spans.push(token.content);
      for (const child of token.children ?? [])
        if (child.type.startsWith('math_')) spans.push(child.content);
    }
    expect(spans).toContain('\\unknown{x}');
  },
);
