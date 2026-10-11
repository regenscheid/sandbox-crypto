// SPDX-License-Identifier: MIT
/**
 * Hash routes, without a router library:
 *
 * - `#/` the projects;
 * - `#/projects/<project>` one project;
 * - `#/projects/<project>/sessions/<session>` one session and its runs;
 * - `#/projects/<project>/documents/<document>` a living document's current revision, and
 *   `…/revisions/<number>` one of its revisions;
 * - `#/projects/<project>/documents/<document>/history` who changed what in it.
 */

export type Route =
  | { view: 'projects' }
  | { view: 'project'; project: string }
  | { view: 'session'; project: string; session: string }
  | { view: 'document'; project: string; document: string; revision: number | null }
  | { view: 'history'; project: string; document: string }
  | { view: 'missing' };

function decoded(part: string): string | null {
  try {
    const value = decodeURIComponent(part);
    return value === '' ? null : value;
  } catch {
    return null;
  }
}

function revision(part: string | undefined): number | null {
  return part !== undefined && /^[1-9][0-9]{0,8}$/.test(part) ? Number(part) : null;
}

export function parseRoute(hash: string): Route {
  const path = hash.replace(/^#/, '');
  if (path === '' || path === '/') return { view: 'projects' };
  const parts = path.replace(/^\//, '').replace(/\/$/, '').split('/');
  if (parts[0] !== 'projects') return { view: 'missing' };
  const project = parts.length >= 2 ? decoded(parts[1]!) : null;
  if (project === null) return { view: 'missing' };
  if (parts.length === 2) return { view: 'project', project };
  const name = parts.length >= 4 ? decoded(parts[3]!) : null;
  if (name === null) return { view: 'missing' };
  if (parts[2] === 'sessions' && parts.length === 4) {
    return { view: 'session', project, session: name };
  }
  if (parts[2] === 'documents') {
    if (parts.length === 4) return { view: 'document', project, document: name, revision: null };
    if (parts.length === 5 && parts[4] === 'history') {
      return { view: 'history', project, document: name };
    }
    const number = parts.length === 6 && parts[4] === 'revisions' ? revision(parts[5]) : null;
    if (number !== null) return { view: 'document', project, document: name, revision: number };
  }
  return { view: 'missing' };
}

export function projectHref(project: string): string {
  return `#/projects/${encodeURIComponent(project)}`;
}

export function sessionHref(project: string, session: string): string {
  return `${projectHref(project)}/sessions/${encodeURIComponent(session)}`;
}

export function documentHref(project: string, document: string, revision?: number): string {
  const base = `${projectHref(project)}/documents/${encodeURIComponent(document)}`;
  return revision === undefined ? base : `${base}/revisions/${revision}`;
}

export function historyHref(project: string, document: string): string {
  return `${documentHref(project, document)}/history`;
}
