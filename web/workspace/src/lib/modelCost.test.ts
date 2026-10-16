// SPDX-License-Identifier: MIT
import { expect, it } from 'vitest';
import type { ModelCost } from './api-types';
import { costByRole, costSource } from './modelCost';

const UNPRICED = { model_info_last_day: null, model_info_total: null };

it('splits the cost by role once more than one role spent', () => {
  const cost: ModelCost = {
    ...UNPRICED,
    last_day: 0.6,
    total: 1.5,
    by_role: [
      { role: 'main', last_day: 0.4, total: 1 },
      { role: 'reviewer', last_day: 0.2, total: 0.45 },
      { role: 'watcher', last_day: null, total: 0.05 },
      { role: 'embeddings', last_day: 0.01, total: 0.02 },
      { role: 'metadata', last_day: null, total: 0.03 },
    ],
  };
  expect(costByRole(cost)).toEqual([
    'The main agent: $0.40 in the last 24 hours, $1.00 in all',
    'The reviewer: $0.20 in the last 24 hours, $0.45 in all',
    'The job watcher: $0 in the last 24 hours, $0.05 in all',
    'Skill embeddings: $0.01 in the last 24 hours, $0.02 in all',
    'Skill metadata: $0 in the last 24 hours, $0.03 in all',
  ]);
});

it('says nothing more when one role spent it all', () => {
  const one: ModelCost = {
    ...UNPRICED,
    last_day: 0.1,
    total: 0.1,
    by_role: [{ role: 'main', last_day: 0.1, total: 0.1 }],
  };
  expect(costByRole(one)).toEqual([]);
  expect(costByRole({ ...UNPRICED, last_day: null, total: null, by_role: [] })).toEqual([]);
});

it("says how much of the cost the gateway's model info priced", () => {
  const cost: ModelCost = { ...UNPRICED, last_day: 0.6, total: 1.5, by_role: [] };
  expect(costSource(cost)).toBe('as the gateway reported it');
  expect(costSource({ ...cost, model_info_last_day: null, model_info_total: 0.25 })).toBe(
    'as the gateway reported it or its model info priced it ($0 and $0.25 of it priced from the model info)',
  );
});
