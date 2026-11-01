// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { authorName, describeAuthor } from './authors';

describe('authors and changes in words', () => {
  it.each([
    [{ kind: 'user' as const, user: 'user' }, 'the user'],
    [{ kind: 'import' as const, user: 'user', import_id: 'import-1' }, 'an import by the user'],
    [{ kind: 'platform' as const, service: 'merge' }, "the platform's merge"],
    [
      { kind: 'worker' as const, environment_id: 'env', incarnation: 'inc' },
      'worker code in environment env, incarnation inc',
    ],
    [
      { kind: 'worker' as const, environment_id: 'env', incarnation: 'inc', session_id: 's' },
      'worker code in environment env, incarnation inc, session s',
    ],
    [
      { kind: 'agent' as const, session_id: 's1', run_id: 'run-1', role: 'main', model: 'm' },
      'agent run run-1 of session s1, m',
    ],
    [
      { kind: 'agent' as const, session_id: 's1', run_id: 'run-2', role: 'reviewer' },
      'reviewer agent run run-2 of session s1',
    ],
  ])('%j', (author, words) => {
    expect(describeAuthor(author)).toBe(words);
  });

  it('names who wrote a revision briefly, for a list', () => {
    expect(authorName({ kind: 'user', user: 'user' })).toBe('You');
    expect(authorName({ kind: 'agent', session_id: 'main', role: 'main', run_id: 'r' })).toBe(
      'Research agent · session main',
    );
    expect(authorName({ kind: 'agent', role: 'reviewer' })).toBe('Reviewer agent');
  });
});
