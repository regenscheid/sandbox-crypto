// SPDX-License-Identifier: MIT
// A finding's statement, remedy and explanations are text an agent wrote that the user
// confirms, dismisses or rebuts, and agents read again (threat model, A3): each is shown
// exactly, every hidden character a code point, where the user decides, and a rendering
// that hides anything has a banner beside it with the links it hides.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import FindingCard from '../views/FindingCard.svelte';
import FindingTransition from '../views/FindingTransition.svelte';
import ResponseBody from '../views/ResponseBody.svelte';
import { exactViews, HIDDEN_CONTENT, hostileMarkup } from './approvalTesting';
import type { Finding, ResponseBody as Body, Transition } from './reviews';
import { hiddenCharacters, hiddenPlaces, visible } from './text';
import { linksIn } from './approval';

function finding(statement: string, remedy: string | null): Finding {
  return {
    finding_id: 'finding-hidden',
    review_id: null,
    severity: 'warn',
    category: 'unsupported_claim',
    statement,
    remedy,
    status: 'claimed fixed',
    revision: 2,
    reflags: 0,
    anchors: [],
  } as unknown as Finding;
}

const card = (statement: string, remedy: string | null = null): string =>
  render(FindingCard, {
    props: {
      projectId: 'p',
      sessionId: 's',
      finding: finding(statement, remedy),
      changed: async () => {},
    },
  }).body;

function body(text: string): Body {
  return {
    source: 'source-1',
    finding: 'finding-hidden',
    response_revision: 2,
    revision: 1,
    body: text,
    sha256: 'a'.repeat(64),
    original_author: { run_id: 'run-main' },
    author: null,
  };
}

function transition(reason: string): Transition {
  return {
    revision: 2,
    status: 'claimed fixed',
    actor: 'main',
    reason,
    payload: { note: reason },
    created: 1_790_000_000,
    response_body: body(reason),
  };
}

/** Whether the server-rendered banner beside a rendering shows for this text. */
const banner = (text: string) => hiddenPlaces(text).length > 0 || linksIn(text).length > 0;

describe('a finding the user decides on', () => {
  it.each(HIDDEN_CONTENT)('shows the statement and remedy exactly, with %s', (_, text) => {
    const html = card(text, text);
    const exact = exactViews(html);
    expect(exact).toEqual([visible(text), visible(text)]);
    for (const view of exact) expect(hiddenCharacters(view)).toEqual([]);
    expect(html.includes('hidden-banner')).toBe(banner(text));
    expect(hostileMarkup(html)).toBe(false);
  });

  it('puts the exact statement before the decision', () => {
    const html = card('The bound is tight.', 'Cite the proof.');
    const decision = html.indexOf('Save user response');
    expect(html.indexOf("The finding's statement, exactly")).toBeLessThan(decision);
    expect(html.indexOf('Its suggested remedy, exactly')).toBeLessThan(decision);
  });
});

describe("an agent's explanation", () => {
  it.each(HIDDEN_CONTENT)('shows a step of the history exactly, with %s', (_, text) => {
    const html = render(FindingTransition, {
      props: { projectId: 'p', sessionId: 's', entry: transition(text) },
    }).body;
    // The reason, then the response body's explanation.
    const exact = exactViews(html);
    expect(exact).toEqual([visible(text), visible(text)]);
    for (const view of exact) expect(hiddenCharacters(view)).toEqual([]);
    expect(html.includes('hidden-banner')).toBe(banner(text));
    expect(hostileMarkup(html)).toBe(false);
  });

  it('shows its recorded evidence and attribution with hidden characters as code points', () => {
    const entry = {
      ...transition('Fixed.'),
      payload: { note: `a${String.fromCodePoint(0x202e)}b` },
    };
    delete entry.response_body;
    const html = render(FindingTransition, {
      props: { projectId: 'p', sessionId: 's', entry },
    }).body;
    expect(hiddenCharacters(html)).toEqual([]);
    expect(html).toContain('a⟦U+202E⟧b');
  });

  it.each(HIDDEN_CONTENT)('shows a response body exactly, with %s', (_, text) => {
    const html = render(ResponseBody, {
      props: { projectId: 'p', sessionId: 's', source: body(text) },
    }).body;
    expect(exactViews(html)).toEqual([visible(text)]);
    expect(html.includes('hidden-banner')).toBe(banner(text));
    expect(hostileMarkup(html)).toBe(false);
  });

  it('renders no text as HTML', () => {
    const views = import.meta.glob(
      [
        '../views/FindingCard.svelte',
        '../views/FindingTransition.svelte',
        '../views/ResponseBody.svelte',
      ],
      { query: '?raw', import: 'default', eager: true },
    );
    expect(Object.keys(views)).toHaveLength(3);
    for (const source of Object.values(views)) expect(String(source)).not.toContain('{@html');
  });
});
