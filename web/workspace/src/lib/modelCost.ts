// SPDX-License-Identifier: MIT
import type { ModelCost, RoleCost } from './api-types';
import { formatCost } from './format';

const ROLES: Record<RoleCost['role'], string> = {
  main: 'The main agent',
  delegated: 'Delegated runs',
  reviewer: 'The reviewer',
  watcher: 'The job watcher',
};

/**
 * The project's model cost by role, a line for each role whose calls the gateway priced:
 * the main agent's runs, delegated runs, the reviewer and the job watcher (lab 40, B5).
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
