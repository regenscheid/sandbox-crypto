// SPDX-License-Identifier: MIT
import { expect, it } from 'vitest';
import type { ModelCost } from './api-types';
import { costByRole } from './modelCost';

it('splits the cost by role once more than one role spent', () => {
  const cost: ModelCost = {
    last_day: 0.6,
    total: 1.5,
    by_role: [
      { role: 'main', last_day: 0.4, total: 1 },
      { role: 'reviewer', last_day: 0.2, total: 0.45 },
      { role: 'watcher', last_day: null, total: 0.05 },
      { role: 'embeddings', last_day: 0.01, total: 0.02 },
    ],
  };
  expect(costByRole(cost)).toEqual([
    'The main agent: $0.40 in the last 24 hours, $1.00 in all',
    'The reviewer: $0.20 in the last 24 hours, $0.45 in all',
    'The job watcher: $0 in the last 24 hours, $0.05 in all',
    'Skill embeddings: $0.01 in the last 24 hours, $0.02 in all',
  ]);
});

it('says nothing more when one role spent it all', () => {
  const one: ModelCost = {
    last_day: 0.1,
    total: 0.1,
    by_role: [{ role: 'main', last_day: 0.1, total: 0.1 }],
  };
  expect(costByRole(one)).toEqual([]);
  expect(costByRole({ last_day: null, total: null, by_role: [] })).toEqual([]);
});
