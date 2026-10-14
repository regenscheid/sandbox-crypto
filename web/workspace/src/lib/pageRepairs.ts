// SPDX-License-Identifier: MIT
/** The page's model repairs, through the workspace API; none outside a browser. */
import { api } from './api';
import { ModelRepairs } from './modelRepairs';

export const pageRepairs = new ModelRepairs(
  typeof window === 'undefined' ? null : (request) => api.mathRepair(request),
);
