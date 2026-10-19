// SPDX-License-Identifier: MIT
// Questions to the user, mounted and used (docs/QUESTIONS.md): what each answer sends, the
// agent's text with what a person may not see shown, a closed question's reason, and the
// message box's hint naming the question a message answers.
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ChatPanel from '../views/ChatPanel.svelte';
import ChatQuestion from '../views/ChatQuestion.svelte';
import type { MessageListing, Question, QuestionListing } from './api-types';
import type { AnswerBody } from './questions';

const ZWSP = String.fromCodePoint(0x200b);
const ASKED = '2026-10-15T12:00:00Z';
const NOW = Date.parse(ASKED) + 5 * 60_000;

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

function show<P extends Record<string, any>>(component: Component<P>, props: P) {
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(mount(component, { target, props }));
  flushSync();
  return target;
}

const button = (root: HTMLElement, words: string) => {
  const found = [...root.querySelectorAll('button')].find((b) =>
    b.textContent?.trim().startsWith(words),
  );
  if (!found) throw new Error(`no button "${words}"`);
  return found as HTMLButtonElement;
};

const question = (fields: Partial<Question> = {}): Question => ({
  question_id: 'qst-000000000000000000000001',
  project: 'p1',
  session: 's1',
  run: 'run-1',
  tool_call: 'call-1',
  text: 'Which curve should sign the release?',
  options: [
    { label: 'Ed25519', detail: 'The common choice' },
    { label: 'Ed448', detail: null },
  ],
  multiple: false,
  state: 'open',
  asked_at: ASKED,
  answer: null,
  closed_at: null,
  reason: null,
  message: null,
  ...fields,
});

function card(fields: Partial<Question> = {}, byMessage = false) {
  const sent: AnswerBody[] = [];
  const answer = vi.fn(async (body: AnswerBody) => {
    sent.push(body);
  });
  const root = show(ChatQuestion, { question: question(fields), now: NOW, byMessage, answer });
  return { root, sent, answer };
}

describe('an open question', () => {
  it('shows its age and the agent text exactly, and sends the option picked', async () => {
    const { root, sent } = card({}, true);
    expect(root.textContent).toContain('asked 5 minutes ago');
    expect(root.textContent).toContain('Which curve should sign the release?');
    expect(root.textContent).toContain('The common choice');
    expect(root.textContent).toContain('write it in the message box');
    button(root, 'Ed448').click();
    await settle();
    expect(sent).toEqual([{ kind: 'picked', picked: ['Ed448'] }]);
    expect(root.textContent).toContain('Your answer is sent');
    expect(root.querySelectorAll('button')).toHaveLength(0);
  });

  it('sends the several chosen, in the order of the options', async () => {
    const { root, sent } = card({ multiple: true });
    expect(button(root, 'Answer with the 0 chosen').disabled).toBe(true);
    const boxes = [...root.querySelectorAll<HTMLInputElement>('input[type=checkbox]')];
    for (const box of [boxes[1]!, boxes[0]!]) {
      box.checked = true;
      box.dispatchEvent(new Event('change', { bubbles: true }));
    }
    flushSync();
    button(root, 'Answer with the 2 chosen').click();
    await settle();
    expect(sent).toEqual([{ kind: 'picked', picked: ['Ed25519', 'Ed448'] }]);
  });

  it('leaves the decision to the agent, or cancels', async () => {
    const decided = card();
    button(decided.root, 'Let the agent decide').click();
    await settle();
    const cancelled = card();
    button(cancelled.root, 'Cancel the question').click();
    await settle();
    expect([...decided.sent, ...cancelled.sent]).toEqual([{ kind: 'decide' }, { kind: 'cancel' }]);
  });

  it('shows what a person may not see in the text and the options as code points', () => {
    const { root } = card({
      text: `Which${ZWSP} curve?`,
      options: [
        { label: `Ed${ZWSP}448`, detail: `safe${ZWSP}` },
        { label: 'Ed25519', detail: null },
      ],
    });
    expect(root.textContent).toContain('⟦U+200B⟧ curve?');
    expect(button(root, 'Ed⟦U+200B⟧448')).toBeTruthy();
    expect(root.textContent).toContain('safe⟦U+200B⟧');
    // The exact text and the options never hold it; only the preview, labelled as one, renders it.
    expect(root.querySelector('.options')!.textContent).not.toContain(ZWSP);
    expect(root.querySelector('pre.exact')!.textContent).not.toContain(ZWSP);
  });

  it('keeps its buttons and shows the failure when the answer is refused', async () => {
    const answer = vi.fn(async () => {
      throw new Error('question qst-1 is answered, not open');
    });
    const root = show(ChatQuestion, { question: question(), now: NOW, answer });
    button(root, 'Let the agent decide').click();
    await settle();
    expect(root.textContent).toContain('is answered, not open');
    expect(button(root, 'Let the agent decide').disabled).toBe(false);
  });
});

describe("options named as an object's own properties", () => {
  it('start unticked, and send the labels ticked', async () => {
    const labels = ['toString', '__proto__', 'constructor', 'hasOwnProperty'];
    const { root, sent } = card({
      multiple: true,
      options: labels.map((label) => ({ label, detail: null })),
    });
    const boxes = [...root.querySelectorAll<HTMLInputElement>('input[type=checkbox]')];
    expect(boxes.map((box) => box.checked)).toEqual([false, false, false, false]);
    expect(button(root, 'Answer with the 0 chosen').disabled).toBe(true);
    boxes[1]!.checked = true;
    boxes[1]!.dispatchEvent(new Event('change', { bubbles: true }));
    flushSync();
    button(root, 'Answer with the 1 chosen').click();
    await settle();
    expect(sent).toEqual([{ kind: 'picked', picked: ['__proto__'] }]);
  });
});

describe('a closed question', () => {
  it('says how it closed, with the reason when no answer came', () => {
    const interrupted = card({
      state: 'interrupted',
      closed_at: ASKED,
      reason: 'the run was interrupted',
    });
    expect(interrupted.root.textContent).toContain(
      'The question was interrupted: the run was interrupted.',
    );
    expect(interrupted.root.querySelectorAll('button')).toHaveLength(0);
    const answered = card({
      state: 'answered',
      closed_at: ASKED,
      answer: { kind: 'free', picked: [], text: `Both${ZWSP}, please.` },
      message: 'm-1',
    });
    expect(answered.root.textContent).toContain('You answered in your own words, with a message.');
    expect(answered.root.textContent).toContain('Both⟦U+200B⟧, please.');
  });
});

describe('the chat panel', () => {
  function serve(questions: Question[], lease: string | null, refuse: unknown = null) {
    const sent: { url: string; body: unknown }[] = [];
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        sent.push({ url, body: JSON.parse(String(init.body)) });
        if (refuse !== null) return new Response(JSON.stringify(refuse), { status: 409 });
        return new Response(JSON.stringify({ question: questions[0] }));
      }
      const run = lease ? { run: lease, started_at: ASKED, renewed_at: ASKED } : null;
      if (url.includes('/questions')) {
        const shown = url.includes('state=open')
          ? questions.filter((question) => question.state === 'open')
          : questions;
        const listing: QuestionListing = { questions: shown, run };
        return new Response(JSON.stringify(listing));
      }
      if (url.includes('/messages')) {
        const listing: MessageListing = { messages: [], run, service: true, environment: null };
        return new Response(JSON.stringify(listing));
      }
      if (url.includes('/recovery/resources'))
        return new Response(JSON.stringify({ requests: [], next_after: null }));
      return new Response(JSON.stringify({ state: 'active', events: [] }));
    });
    vi.stubGlobal('fetch', fetch);
    return sent;
  }

  it('names the question a message will answer, the oldest of its run', async () => {
    const stale = question({ question_id: 'qst-000000000000000000000000', run: 'run-0' });
    const first = question({ question_id: 'qst-000000000000000000000001', text: 'First?' });
    const second = question({ question_id: 'qst-000000000000000000000002', text: 'Second?' });
    const sent = serve([stale, first, second], 'run-1');
    const root = show(ChatPanel, { projectId: 'p1', sessionId: 's1', closed: false });
    await settle();
    const hint = root.querySelector('.hint')!;
    expect(hint.textContent).toContain('Your message will answer: First?');
    expect(hint.querySelector('q')!.textContent).toBe('First?');
    expect(hint.textContent).toContain('the oldest of the 2 open questions');
    expect(button(root, 'Send as the answer')).toBeTruthy();
    const cards = [...root.querySelectorAll('section.question')];
    expect(cards).toHaveLength(3);
    // The stale run's question takes no answer: the server would refuse it (run_ended).
    expect(cards[0]!.textContent).toContain('Its run no longer holds the session');
    expect(cards[0]!.querySelectorAll('button')).toHaveLength(0);
    button(cards[1] as HTMLElement, 'Let the agent decide').click();
    await settle();
    expect(sent).toEqual([
      {
        url: '/api/v0/projects/p1/sessions/s1/questions/qst-000000000000000000000001/answer',
        body: { kind: 'decide' },
      },
    ]);
  });

  it('gives no hint while no run that holds the session waits for an answer', async () => {
    serve([question({ run: 'run-0' })], null);
    const root = show(ChatPanel, { projectId: 'p1', sessionId: 's1', closed: false });
    await settle();
    expect(root.querySelector('.hint')).toBeNull();
    expect(button(root, 'Send')).toBeTruthy();
  });

  function typeMessage(root: HTMLElement, text: string): void {
    const box = root.querySelector('textarea')!;
    box.value = text;
    box.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
  }

  it('sends the question it showed, and keeps the draft when that question changed', async () => {
    const shown = question({ text: 'First?' });
    const changed = {
      error: {
        kind: 'conflict',
        message: 'the question this message answers has changed: read the chat again',
        details: { reason: 'question_changed', question: null },
      },
    };
    const sent = serve([shown], 'run-1', changed);
    const root = show(ChatPanel, { projectId: 'p1', sessionId: 's2', closed: false });
    await settle();
    typeMessage(root, 'Ed448, please.');
    button(root, 'Send as the answer').click();
    await settle();
    expect(sent).toEqual([
      {
        url: '/api/v0/projects/p1/sessions/s2/messages',
        body: {
          message_id: expect.any(String),
          text: 'Ed448, please.',
          question_id: shown.question_id,
        },
      },
    ]);
    expect(root.textContent).toContain('the question this message answers has changed');
    expect(root.querySelector('textarea')!.value).toBe('Ed448, please.');
  });

  it('holds an answer to the 16,000 characters an answer may have', async () => {
    serve([question()], 'run-1');
    const root = show(ChatPanel, { projectId: 'p1', sessionId: 's3', closed: false });
    await settle();
    typeMessage(root, 'é'.repeat(16_000));
    expect(button(root, 'Send as the answer').disabled).toBe(false);
    typeMessage(root, 'é'.repeat(16_001));
    expect(button(root, 'Send as the answer').disabled).toBe(true);
    expect(root.textContent).toContain('an answer holds at most');
  });
});
