// SPDX-License-Identifier: MIT
/**
 * Hash routes, without a router library:
 *
 * - `#/` the projects;
 * - `#/projects/<project>` one project;
 * - `#/projects/<project>/sessions/<session>` one session and its runs.
 */

export type Route =
  | { view: 'projects' }
  | { view: 'project'; project: string }
  | { view: 'session'; project: string; session: string }
  | { view: 'missing' };

function decoded(part: string): string | null {
  try {
    const value = decodeURIComponent(part);
    return value === '' ? null : value;
  } catch {
    return null;
  }
}

export function parseRoute(hash: string): Route {
  const path = hash.replace(/^#/, '');
  if (path === '' || path === '/') return { view: 'projects' };
  const parts = path.replace(/^\//, '').replace(/\/$/, '').split('/');
  if (parts[0] !== 'projects') return { view: 'missing' };
  const project = parts.length >= 2 ? decoded(parts[1]!) : null;
  if (parts.length === 2 && project !== null) return { view: 'project', project };
  const session = parts.length === 4 && parts[2] === 'sessions' ? decoded(parts[3]!) : null;
  if (project !== null && session !== null) return { view: 'session', project, session };
  return { view: 'missing' };
}

export function projectHref(project: string): string {
  return `#/projects/${encodeURIComponent(project)}`;
}

export function sessionHref(project: string, session: string): string {
  return `${projectHref(project)}/sessions/${encodeURIComponent(session)}`;
}
