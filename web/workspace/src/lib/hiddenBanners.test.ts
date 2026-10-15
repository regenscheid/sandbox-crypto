// SPDX-License-Identifier: MIT
// Where the user reads text agents wrote, in a comment or the chat, or writes text agents
// read, in a text box (threat model, A3): the rendering stays, and a banner beside it
// names each character the user may not see and each link the rendering hides; tool
// arguments and results, and raw document text, show hidden characters as code points.
import { render } from 'svelte/server';
import { afterEach, describe, expect, it } from 'vitest';
import AnnotationThread from '../views/AnnotationThread.svelte';
import ChatRun from '../views/ChatRun.svelte';
import DocumentEditor from '../views/DocumentEditor.svelte';
import type { CommentThread, DocumentRevision } from './api';
import { linksIn, renderingHides } from './approval';
import { exactViews, HIDDEN_CONTENT, hostileMarkup } from './approvalTesting';
import type { RunEvent } from './chat';
import { documentDrafts } from './drafts';
import { NO_MACROS } from './macros';
import { hiddenCharacters, hiddenPlaces, visible } from './text';

/** Whether a banner shows beside a rendering of the text. */
const banner = (text: string) =>
  hiddenPlaces(text).length > 0 || linksIn(text).length > 0 || renderingHides(text).length > 0;
const banners = (html: string) => (html.match(/class="hidden-banner/g) ?? []).length;

function thread(body: string): CommentThread {
  return {
    thread_id: 'ann-hidden',
    revision: 2,
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
        entry_id: 'cmt-agent',
        number: 1,
        kind: 'reply',
        author: { kind: 'agent', session_id: 's', run_id: 'main', role: 'main' },
        body,
        payload: {},
        created_at: 2,
      },
    ],
  };
}

describe('a comment thread', () => {
  it.each(HIDDEN_CONTENT)('has a banner and an exact view of a comment with %s', (_, text) => {
    const html = render(AnnotationThread, {
      props: {
        projectId: 'p',
        documentId: 'doc',
        thread: thread(text),
        sessions: [],
        macros: NO_MACROS,
        changed: async () => {},
      },
    }).body;
    expect(banners(html)).toBe(1); // every kind hides something from a rendering
    expect(exactViews(html)).toEqual([visible(text)]);
  });

  it("shows the anchor's quote and context with hidden characters as code points", () => {
    const value = thread('Plain.');
    const override = String.fromCodePoint(0x202e);
    value.anchor = {
      ...value.anchor,
      before: `b${override}`,
      quote: `q${override}`,
      after: `a${override}`,
    };
    const html = render(AnnotationThread, {
      props: {
        projectId: 'p',
        documentId: 'doc',
        thread: value,
        sessions: [],
        macros: NO_MACROS,
        changed: async () => {},
      },
    }).body;
    expect(hiddenCharacters(html)).toEqual([]);
    for (const part of ['b', 'q', 'a']) expect(html).toContain(`${part}⟦U+202E⟧`);
  });

  it.each(HIDDEN_CONTENT)('keeps the rendering and adds a banner, with %s', (_, text) => {
    const html = render(AnnotationThread, {
      props: {
        projectId: 'p',
        documentId: 'doc',
        thread: thread(text),
        sessions: [],
        macros: NO_MACROS,
        changed: async () => {},
      },
    }).body;
    expect(banners(html)).toBe(banner(text) ? 1 : 0);
    for (const place of hiddenPlaces(text)) expect(html).toContain(`⟦${place.code}⟧`);
    for (const link of linksIn(text)) expect(html).toContain(link.address);
    expect(hostileMarkup(html)).toBe(false);
  });
});

const event = (seq: number, kind: RunEvent['kind'], data: RunEvent['data']): RunEvent => ({
  seq,
  at: '2026-10-14T12:00:00Z',
  kind,
  data,
});
const chat = (events: RunEvent[]) =>
  render(ChatRun, { props: { run: { run: 'r1', state: 'active', events } } }).body;

describe('the chat', () => {
  it.each(HIDDEN_CONTENT)(
    "keeps the agent's message rendered with a banner, with %s",
    (_, text) => {
      const html = chat([event(1, 'assistant', { text, tool_calls: [] })]);
      expect(banners(html)).toBe(banner(text) ? 1 : 0);
      for (const place of hiddenPlaces(text)) expect(html).toContain(`⟦${place.code}⟧`);
      expect(hostileMarkup(html)).toBe(false);
    },
  );

  it('shows tool arguments and results with hidden characters as code points', () => {
    const override = String.fromCodePoint(0x202e),
      space = String.fromCodePoint(0x200b),
      escape = String.fromCodePoint(0x1b);
    const html = chat([
      event(1, 'assistant', {
        text: '',
        tool_calls: [{ name: 'shell', arguments: `{"cmd": "a${override}b"}` }],
      }),
      event(2, 'tool', {
        name: 'shell',
        outcome: 'ok',
        result: `${escape}[31mred${escape}[0m and a${space}b`,
      }),
    ]);
    expect(hiddenCharacters(html)).toEqual([]);
    expect(html).toContain('a⟦U+202E⟧b');
    expect(html).toContain('red and a⟦U+200B⟧b');
  });
});

function revision(text: string): DocumentRevision {
  return {
    document: { document_id: 'doc-hidden', head: 3, name: 'notes', scope: 'project' },
    revision: { number: 3 },
    text,
    matched: true,
    blocks: [],
  } as unknown as DocumentRevision;
}

describe("the editor's text boxes", () => {
  afterEach(() => documentDrafts.clear('p:doc-hidden'));

  it.each(HIDDEN_CONTENT)('names each hidden character of a draft, with %s', (_, text) => {
    documentDrafts.save('p:doc-hidden', {
      base: 3,
      text,
      reason: 'User edit',
      block: null,
      retained: text,
    });
    const html = render(DocumentEditor, {
      props: { projectId: 'p', found: revision(text), refreshed: () => {} },
    }).body;
    // The draft and the retained draft: raw text, where every link shows as written.
    expect(banners(html)).toBe(hiddenPlaces(text).length > 0 ? 2 : 0);
    for (const place of hiddenPlaces(text)) expect(html).toContain(`⟦${place.code}⟧`);
  });
});

it('puts the banner beside the run view and the ledger entry renderings', async () => {
  const run = (await import('../views/RunView.svelte?raw')).default;
  expect(run.match(/<HiddenBanner text=\{step.text\} links \/>/g)).toHaveLength(3);
  const entry = (await import('../views/LedgerEntryBody.svelte?raw')).default;
  expect(entry).toContain('<HiddenBanner text={entry.statement} links />');
});

it('renders no text as HTML', () => {
  const views = import.meta.glob(
    [
      '../views/AnnotationThread.svelte',
      '../views/ChatPanel.svelte',
      '../views/ChatRun.svelte',
      '../views/CommentBodyEditor.svelte',
      '../views/DocumentComments.svelte',
      '../views/DocumentEditor.svelte',
      '../views/HiddenBanner.svelte',
      '../views/LedgerEntryBody.svelte',
      '../views/RunView.svelte',
    ],
    { query: '?raw', import: 'default', eager: true },
  );
  expect(Object.keys(views)).toHaveLength(9);
  for (const source of Object.values(views)) expect(String(source)).not.toContain('{@html');
});
