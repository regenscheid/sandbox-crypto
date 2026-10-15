// SPDX-License-Identifier: MIT
/**
 * Questions the session's main agent asks the user (docs/QUESTIONS.md; plans and
 * delegation, PD-23 to PD-26), read with the chat and answered in the chat panel. A
 * question's text and options are the agent's: the panel shows them through
 * `ApprovalText` and `visible`, never as HTML. A message the user writes while the run that
 * holds the session waits answers that run's oldest open question (the server's rule).
 */
import type { Question, QuestionAnswer, QuestionListing, RunLease } from './api-types';
import { get, post } from './http';
import { visible } from './text';

export type { Question };

/** What the user sends to answer: options picked, their own words, or no answer. */
export type AnswerBody =
  | { kind: 'picked'; picked: string[] }
  | { kind: 'free'; text: string }
  | { kind: 'decide' }
  | { kind: 'cancel' };

const part = encodeURIComponent;
const at = (project: string, session: string) =>
  `/projects/${part(project)}/sessions/${part(session)}/questions`;

export const questionsApi = {
  list: (project: string, session: string, signal?: AbortSignal) =>
    get<QuestionListing>(at(project, session), { signal }),
  answer: (project: string, session: string, question: string, body: AnswerBody) =>
    post<QuestionAnswer>(`${at(project, session)}/${part(question)}/answer`, body),
};

/** The open questions, oldest first. */
export function openQuestions(questions: readonly Question[]): Question[] {
  return questions.filter((question) => question.state === 'open');
}

/**
 * The question a message to the session answers now: the oldest open question of the run
 * that holds the session. Null when that run waits for no answer, or no run holds the
 * session: a message then waits in the queue as any other.
 */
export function answering(questions: readonly Question[], lease: RunLease | null): Question | null {
  if (lease === null) return null;
  return openQuestions(questions).find((question) => question.run === lease.run) ?? null;
}

const MINUTE = 60_000;

/** How long ago a question was asked, in words: "just now", "5 minutes ago". */
export function ageText(asked: string, now: number): string {
  const elapsed = now - Date.parse(asked);
  if (!Number.isFinite(elapsed) || elapsed < MINUTE) return 'just now';
  const units: [number, string][] = [
    [24 * 60 * MINUTE, 'day'],
    [60 * MINUTE, 'hour'],
    [MINUTE, 'minute'],
  ];
  for (const [size, unit] of units) {
    if (elapsed >= size) {
      const count = Math.floor(elapsed / size);
      return `${count} ${unit}${count === 1 ? '' : 's'} ago`;
    }
  }
  return 'just now';
}

/** The most characters of a question the message box's hint shows. */
export const HINT_CHARS = 120;

/**
 * A question's text for the message box's hint: its first line, cut to `HINT_CHARS`,
 * with every character a person may not see shown as its code point, and an ellipsis
 * when there is more.
 */
export function hintText(text: string): string {
  const whole = text.trim();
  const line = whole.split('\n')[0]!.trim();
  const characters = [...line];
  const cut = characters.length > HINT_CHARS;
  const shown = cut ? characters.slice(0, HINT_CHARS - 1).join('') : line;
  return visible(shown) + (cut || line !== whole ? '…' : '');
}

/** How a closed question closed, in a sentence. */
export function closedText(question: Question): string {
  const answer = question.answer;
  if (question.state === 'answered' && answer !== null) {
    if (answer.kind === 'picked') {
      const labels = answer.picked.map((label) => `"${visible(label)}"`).join(', ');
      return `You picked ${labels}.`;
    }
    if (answer.kind === 'free')
      return question.message === null
        ? 'You answered in your own words.'
        : 'You answered in your own words, with a message.';
    return 'You left the decision to the agent.';
  }
  if (answer?.kind === 'cancel') return 'You cancelled the question.';
  const reason = question.reason === null ? '' : `: ${visible(question.reason)}`;
  return question.state === 'interrupted'
    ? `The question was interrupted${reason}.`
    : `The question was cancelled${reason}.`;
}
