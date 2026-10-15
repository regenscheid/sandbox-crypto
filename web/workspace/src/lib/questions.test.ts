// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { Question } from './api-types';
import { ChatFeed } from './chat';
import { ageText, answering, closedText, HINT_CHARS, hintText, openQuestions } from './questions';

const ZWSP = String.fromCodePoint(0x200b);

const question = (fields: Partial<Question> = {}): Question => ({
  question_id: 'qst-000000000000000000000001',
  project: 'p1',
  session: 's1',
  run: 'run-1',
  tool_call: 'call-1',
  text: 'Which curve?',
  options: [
    { label: 'Ed25519', detail: 'The common choice' },
    { label: 'Ed448', detail: null },
  ],
  multiple: false,
  state: 'open',
  asked_at: '2026-10-15T12:00:00Z',
  answer: null,
  closed_at: null,
  reason: null,
  message: null,
  ...fields,
});

const lease = (run: string) => ({ run, started_at: '', renewed_at: '' });

describe('the question a message answers', () => {
  it('is the oldest open question of the run that holds the session', () => {
    const stale = question({ question_id: 'q0', run: 'run-0' });
    const first = question({ question_id: 'q1' });
    const second = question({ question_id: 'q2' });
    const done = question({ question_id: 'q3', state: 'answered' });
    const all = [done, stale, first, second];
    expect(openQuestions(all).map((q) => q.question_id)).toEqual(['q0', 'q1', 'q2']);
    expect(answering(all, lease('run-1'))?.question_id).toBe('q1');
    expect(answering(all, lease('run-2'))).toBeNull();
    expect(answering(all, null)).toBeNull(); // no run holds the session: it is queued
  });
});

describe('a question in words', () => {
  it('says how long ago it was asked', () => {
    const asked = Date.parse('2026-10-15T12:00:00Z');
    expect(ageText('2026-10-15T12:00:00Z', asked + 59_000)).toBe('just now');
    expect(ageText('2026-10-15T12:00:00Z', asked + 60_000)).toBe('1 minute ago');
    expect(ageText('2026-10-15T12:00:00Z', asked + 125 * 60_000)).toBe('2 hours ago');
    expect(ageText('2026-10-15T12:00:00Z', asked + 3 * 86_400_000)).toBe('3 days ago');
    expect(ageText('not a time', asked)).toBe('just now');
  });

  it('gives the hint the first line, cut short, with what a person may not see shown', () => {
    expect(hintText('  Which curve?  ')).toBe('Which curve?');
    expect(hintText('Which curve?\nThe review needs one.')).toBe('Which curve?…');
    const long = 'x'.repeat(HINT_CHARS + 5);
    expect([...hintText(long)].length).toBe(HINT_CHARS);
    expect(hintText(`Pick${ZWSP} one`)).toBe('Pick⟦U+200B⟧ one');
  });

  it('says how a closed question closed, and why when no answer came', () => {
    const answered = (answer: Question['answer'], message: string | null = null) =>
      closedText(question({ state: 'answered', answer, message }));
    expect(answered({ kind: 'picked', picked: ['Ed25519', 'Ed448'], text: null })).toBe(
      'You picked "Ed25519", "Ed448".',
    );
    expect(answered({ kind: 'free', picked: [], text: 'Both' })).toBe(
      'You answered in your own words.',
    );
    expect(answered({ kind: 'free', picked: [], text: 'Both' }, 'm-1')).toBe(
      'You answered in your own words, with a message.',
    );
    expect(answered({ kind: 'decide', picked: [], text: null })).toBe(
      'You left the decision to the agent.',
    );
    expect(
      closedText(
        question({
          state: 'cancelled',
          answer: { kind: 'cancel', picked: [], text: null },
          reason: 'the user cancelled it',
        }),
      ),
    ).toBe('You cancelled the question.');
    expect(closedText(question({ state: 'cancelled', reason: 'the run was cancelled' }))).toBe(
      'The question was cancelled: the run was cancelled.',
    );
    expect(closedText(question({ state: 'interrupted', reason: 'the run was interrupted' }))).toBe(
      'The question was interrupted: the run was interrupted.',
    );
    expect(
      closedText(
        question({
          state: 'answered',
          answer: { kind: 'picked', picked: [`a${ZWSP}`], text: null },
        }),
      ),
    ).toBe('You picked "a⟦U+200B⟧".');
  });
});

describe('the chat feed', () => {
  it("reads the session's questions with each poll, when it can", async () => {
    let listed = [question()];
    const reads = {
      messages: async () => ({
        messages: [],
        run: lease('run-1'),
        service: true,
        environment: null,
      }),
      events: async () => ({ state: 'active' as const, events: [] }),
    };
    const feed = new ChatFeed({
      ...reads,
      questions: async () => ({ questions: listed, run: null }),
    });
    expect((await feed.read()).questions).toEqual(listed);
    listed = [question({ state: 'answered' })];
    expect((await feed.read()).questions[0]!.state).toBe('answered');
    expect((await new ChatFeed(reads).read()).questions).toEqual([]);
  });
});
