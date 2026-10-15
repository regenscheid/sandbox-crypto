// SPDX-License-Identifier: MIT
// The user's definition or revision of a frontier category: the body sent, why the server
// would refuse one, the exact summary confirmed, what a revision changes, and conflicts.
import { describe, expect, it } from 'vitest';
import { ApiError } from './api';
import type { Category } from './api-types';
import {
  categoryText,
  changesText,
  conflictText,
  definitionBody,
  draftOf,
  emptyDraft,
  formProblem,
  summaryText,
  type CategoryBody,
} from './frontierForm';

const category = (fields: Partial<Category> = {}): Category => ({
  category: 'key-recovery',
  revision: 2,
  head: 2,
  target: 'Kyber-768',
  kind: 'key recovery cost',
  measures: [
    { name: 'time', unit: 'bits', direction: 'lower' },
    { name: 'memory', unit: 'bits', direction: 'lower' },
  ],
  conditions: ['attack'],
  author: { kind: 'user', user: 'owner' },
  reason: 'compare attacks',
  operation_id: 'op-1',
  created: '2026-10-15T00:00:00Z',
  replayed: false,
  ...fields,
});

const body = (fields: Partial<CategoryBody> = {}): CategoryBody => ({
  category: 'key-recovery',
  target: 'Kyber-768',
  kind: 'key recovery cost',
  measures: [{ name: 'time', unit: 'bits', direction: 'lower' }],
  conditions: ['attack'],
  reason: 'why',
  ...fields,
});

describe('the body sent', () => {
  it('names the revision read only for a revision, trims names and drops blank keys', () => {
    const draft = {
      ...emptyDraft(),
      category: ' key-recovery ',
      target: ' Kyber ',
      kind: 'cost',
      measures: [{ name: ' time ', unit: ' bits ', direction: 'higher' as const }],
      conditions: [' attack ', '  ', 'model'],
      reason: 'r',
    };
    expect(definitionBody(draft, null)).toEqual({
      category: 'key-recovery',
      target: ' Kyber ',
      kind: 'cost',
      measures: [{ name: 'time', unit: ' bits ', direction: 'higher' }],
      conditions: ['attack', 'model'],
      reason: 'r',
    });
    expect(definitionBody(draftOf(category()), category()).expected).toBe(2);
  });

  it('starts a revision from the category as it reads, with no reason', () => {
    const draft = draftOf(category());
    expect(draft).toMatchObject({ category: 'key-recovery', conditions: ['attack'], reason: '' });
    draft.measures[0]!.unit = 'changed';
    expect(category().measures[0]!.unit).toBe('bits');
  });
});

describe('why the server would refuse a definition', () => {
  it.each([
    [body({ category: '-x' }), "The category's name is"],
    [body({ category: 'a b' }), "The category's name is"],
    [body({ target: '  ' }), "Give the category's target."],
    [body({ kind: '' }), "Give the category's kind of result."],
    [body({ reason: '\n' }), "Give the category's reason."],
    [body({ target: 'x'.repeat(4097) }), 'The target is longer than 4096 characters.'],
    [body({ measures: [] }), 'Name at least one measure.'],
    [
      body({
        measures: Array.from({ length: 17 }, (_, n) => ({
          name: `m${n}`,
          unit: 'u',
          direction: 'lower' as const,
        })),
      }),
      'Name at most 16 measures.',
    ],
    [body({ measures: [{ name: 'a/b', unit: 'u', direction: 'lower' }] }), "A measure's name is"],
    [
      body({ measures: [{ name: 'time', unit: ' ', direction: 'lower' }] }),
      "Give measure time's unit.",
    ],
    [
      body({ measures: [{ name: 'time', unit: 'u'.repeat(513), direction: 'lower' }] }),
      "Measure time's unit is longer than 512 characters.",
    ],
    [body({ conditions: ['a b'] }), 'A condition key is'],
    [body({ conditions: Array.from({ length: 17 }, (_, n) => `k${n}`) }), 'Name at most 16'],
    [
      body({
        measures: [
          { name: 'time', unit: 'u', direction: 'lower' },
          { name: 'time', unit: 'v', direction: 'higher' },
        ],
      }),
      'Name each measure once.',
    ],
    [body({ conditions: ['Model', 'model'] }), 'Name each condition key once'],
  ])('%#: %s', (value, words) => {
    expect(formProblem(value)).toContain(words);
  });

  it('takes a well-formed definition', () => {
    expect(formProblem(body())).toBeNull();
    expect(formProblem(body({ conditions: [] }))).toBeNull();
  });
});

describe('what the user confirms', () => {
  it('is exactly what will be saved', () => {
    expect(summaryText(body({ expected: 2, conditions: [] }))).toBe(
      [
        'Category: key-recovery, revising r2',
        'Target: Kyber-768',
        'Kind of result: key recovery cost',
        'Measures:',
        '  time, in bits, lower is better',
        'Condition keys: none',
        'Reason: why',
      ].join('\n'),
    );
    expect(summaryText(body()).split('\n')[0]).toBe('Category: key-recovery (new)');
    expect(categoryText(category()).split('\n')).toEqual([
      'Category: key-recovery, r2',
      'Target: Kyber-768',
      'Kind of result: key recovery cost',
      'Measures:',
      '  time, in bits, lower is better',
      '  memory, in bits, lower is better',
      'Condition keys: attack',
      'Its reason: compare attacks',
    ]);
  });

  it('names what a revision changes, as the frontier says it', () => {
    const revised = body({
      target: 'Kyber-1024',
      measures: [
        { name: 'time', unit: 'bits', direction: 'higher' },
        { name: 'qubits', unit: 'qubits', direction: 'lower' },
      ],
      conditions: ['model'],
    });
    expect(changesText(category(), revised)).toEqual([
      'the target',
      "measure time's direction: higher is better",
      'measure qubits added, lower is better',
      'measure memory removed',
      'condition key model added',
      'condition key attack removed',
    ]);
    expect(changesText(category(), definitionBody(draftOf(category()), category()))).toEqual([]);
  });
});

describe('a conflict', () => {
  const conflict = (revision?: number) =>
    new ApiError(409, 'conflict', 'x', revision === undefined ? {} : { revision });

  it('says the category exists, or has moved on, and that nothing was saved', () => {
    expect(conflictText(conflict(3), body())).toBe(
      'A category named key-recovery exists already, at r3: nothing was saved. Your draft is ' +
        'kept; read the category as it is now, below.',
    );
    expect(conflictText(conflict(4), body({ expected: 2 }))).toMatch(
      /^key-recovery is at r4 now, not the r2 you read: nothing was saved/,
    );
    expect(conflictText(conflict(), body({ expected: 2 }))).toMatch(/at a newer revision now/);
    expect(conflictText(new ApiError(400, 'invalid_request', 'x'), body())).toBeNull();
  });
});
