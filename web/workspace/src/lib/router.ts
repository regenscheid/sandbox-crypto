// SPDX-License-Identifier: MIT
/**
 * Hash routes, without a router library:
 *
 * - `#/` the projects;
 * - `#/projects/<project>` one project;
 * - `#/projects/<project>/sessions/<session>` one session and its runs;
 * - `#/projects/<project>/documents/<document>` a living document's current revision,
 *   `…/revisions/<number>` one of its revisions, and `…/revisions/<number>/blocks/<block>`
 *   that revision with one block in focus (a finding's anchor, say);
 * - `#/projects/<project>/documents/<document>/history` who changed what in it;
 * - `#/projects/<project>/artifacts/<artifact>/revisions/<number>/<sha256>` a published
 *   PDF in the viewer;
 * - `#/projects/<project>/ledger` the project's ledger as a board,
 *   `#/projects/<project>/ledger/issues` its issues and what they concern, and
 *   `#/projects/<project>/ledger/<entry>` one entry by its handle or ID, with
 *   `…/revisions/<number>` for an earlier revision.
 */

export type Route =
  | { view: 'projects' }
  | { view: 'project'; project: string }
  | { view: 'session'; project: string; session: string }
  | {
      view: 'document';
      project: string;
      document: string;
      revision: number | null;
      block: string | null;
    }
  | { view: 'history'; project: string; document: string }
  | { view: 'pdf'; project: string; artifact: string; revision: number; sha256: string }
  | { view: 'ledger'; project: string }
  | { view: 'issues'; project: string }
  | { view: 'entry'; project: string; entry: string; revision: number | null }
  | { view: 'missing' };

/** A ledger entry named by its ID or its handle, as the server takes it. */
export const ENTRY_NAME = /^(?:entry-[0-9a-f]{24}|(?:I|C|R|A|D|T|REF)-[1-9][0-9]{0,8})$/;

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
  if (parts[2] === 'ledger') return ledgerRoute(project, parts.slice(3));
  const name = parts.length >= 4 ? decoded(parts[3]!) : null;
  if (name === null) return { view: 'missing' };
  if (parts[2] === 'sessions' && parts.length === 4) {
    return { view: 'session', project, session: name };
  }
  if (parts[2] === 'documents') {
    if (parts.length === 4)
      return { view: 'document', project, document: name, revision: null, block: null };
    if (parts.length === 5 && parts[4] === 'history') {
      return { view: 'history', project, document: name };
    }
    const number =
      (parts.length === 6 || parts.length === 8) && parts[4] === 'revisions'
        ? revision(parts[5])
        : null;
    const block =
      parts.length === 8 && parts[6] === 'blocks' && /^b[0-9]{1,9}$/.test(parts[7]!)
        ? parts[7]!
        : null;
    if (number !== null && (parts.length === 6 || block !== null))
      return { view: 'document', project, document: name, revision: number, block };
  }
  if (parts[2] === 'artifacts' && parts.length === 7 && parts[4] === 'revisions') {
    const number = revision(parts[5]);
    const sha256 = parts[6]!;
    if (number !== null && /^[0-9a-f]{64}$/.test(sha256)) {
      return { view: 'pdf', project, artifact: name, revision: number, sha256 };
    }
  }
  return { view: 'missing' };
}

/** The board, or one entry: `<entry>` or `<entry>/revisions/<number>` after `ledger/`. */
function ledgerRoute(project: string, rest: string[]): Route {
  if (rest.length === 0) return { view: 'ledger', project };
  if (rest.length === 1 && rest[0] === 'issues') return { view: 'issues', project };
  const entry = rest[0]!;
  if (!ENTRY_NAME.test(entry)) return { view: 'missing' };
  if (rest.length === 1) return { view: 'entry', project, entry, revision: null };
  const number = rest.length === 3 && rest[1] === 'revisions' ? revision(rest[2]) : null;
  return number === null
    ? { view: 'missing' }
    : { view: 'entry', project, entry, revision: number };
}

export function projectHref(project: string): string {
  return `#/projects/${encodeURIComponent(project)}`;
}

export function sessionHref(project: string, session: string): string {
  return `${projectHref(project)}/sessions/${encodeURIComponent(session)}`;
}

export function documentHref(
  project: string,
  document: string,
  revision?: number,
  block?: string,
): string {
  const base = `${projectHref(project)}/documents/${encodeURIComponent(document)}`;
  if (revision === undefined) return base;
  const shown = `${base}/revisions/${revision}`;
  return block === undefined ? shown : `${shown}/blocks/${encodeURIComponent(block)}`;
}

export function historyHref(project: string, document: string): string {
  return `${documentHref(project, document)}/history`;
}

export function pdfHref(project: string, artifact: string, revision: number, sha256: string) {
  return `${projectHref(project)}/artifacts/${encodeURIComponent(artifact)}/revisions/${revision}/${sha256}`;
}

export function ledgerHref(project: string): string {
  return `${projectHref(project)}/ledger`;
}

export function issuesHref(project: string): string {
  return `${ledgerHref(project)}/issues`;
}

/** An entry by its handle or ID; at `revision`, that revision. */
export function ledgerEntryHref(project: string, entry: string, revision?: number): string {
  const base = `${ledgerHref(project)}/${encodeURIComponent(entry)}`;
  return revision === undefined ? base : `${base}/revisions/${revision}`;
}
