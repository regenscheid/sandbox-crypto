// SPDX-License-Identifier: MIT
import type { ModelCost, RoleCost } from './api-types';
import { formatCost } from './format';

const ROLES: Record<RoleCost['role'], string> = {
  main: 'The main agent',
  delegated: 'Delegated runs',
  reviewer: 'The reviewer',
  watcher: 'The job watcher',
  embeddings: 'Skill embeddings',
  metadata: 'Skill metadata',
};

/**
 * Where the project's cost total came from: as the gateway reported it, and, when its
 * model info priced calls it reported no cost for (lab 43, B2), how much of the last
 * 24 hours' and of all of it was priced so.
 */
export function costSource(cost: ModelCost): string {
  const total = cost.model_info_total ?? null;
  if (total === null) return 'as the gateway reported it';
  const day = cost.model_info_last_day ?? 0;
  return (
    `as the gateway reported it or its model info priced it ` +
    `(${formatCost(day)} and ${formatCost(total)} of it priced from the model info)`
  );
}

/**
 * The project's model cost by role, a line for each role whose calls the gateway priced:
 * the main agent's runs, delegated runs, the reviewer, the job watcher (lab 40, B5), the
 * skills' embeddings and the import's model pass.
 * Nothing when a single role spent it all, which the total already says.
 */
export function costByRole(cost: ModelCost): string[] {
  if (cost.by_role.length < 2) return [];
  return cost.by_role.map(
    (part) =>
      `${ROLES[part.role]}: ${formatCost(part.last_day ?? 0)} in the last 24 hours, ` +
      `${formatCost(part.total)} in all`,
  );
}
