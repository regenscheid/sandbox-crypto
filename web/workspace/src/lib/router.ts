// SPDX-License-Identifier: MIT
/**
 * Hash routes, without a router library:
 *
 * - `#/` the projects;
 * - `#/projects/<project>` one project;
 * - `#/projects/<project>/sessions/<session>` one session and its runs, and
 *   `…/findings/<finding>` one of its review findings, as a ledger entry links to it, and
 *   `…/reviews/<review>` the session with one of its reviews chosen, as a quote's citation
 *   check links to it, `…/agents/<delegation>` the session with one delegation's card open
 *   in its Agents panel, `…/tree` its run tree and `…/runs/<run>` one run's events;
 * - `#/projects/<project>/documents/<document>` a living document's current revision,
 *   `…/revisions/<number>` one of its revisions, and `…/revisions/<number>/blocks/<block>`
 *   that revision with one block in focus (a finding's anchor, say);
 * - `#/projects/<project>/documents/<document>/history` who changed what in it;
 * - `#/projects/<project>/artifacts/<artifact>/revisions/<number>/<sha256>` a published
 *   PDF in the viewer, and `…/pages/<page>` opened at a page (a quote's);
 * - `#/projects/<project>/files/<session>/<run>` the project's artifacts that a run
 *   recorded, as a delegation's files link to them;
 * - `#/projects/<project>/memory` the project memory: its entries and the proposals that
 *   wait for the user;
 * - `#/projects/<project>/monitor` the project's monitor figures (D103): calls, approvals,
 *   holds and drift, spend and tokens, sandbox tiers and canaries;
 * - `#/projects/<project>/ledger` the project's ledger browser,
 *   `#/projects/<project>/ledger/issues` its issues and what they concern, and
 *   `#/projects/<project>/ledger/<entry>` one entry by its handle or ID, with
 *   `…/revisions/<number>` for an earlier revision;
 * - `#/projects/<project>/frontier` the project's frontier categories, and
 *   `#/projects/<project>/frontier/<category>` one category's frontier.
 */

export type Route =
  | { view: 'projects' }
  | { view: 'project'; project: string }
  | { view: 'session'; project: string; session: string }
  | { view: 'review'; project: string; session: string; review: string }
  | { view: 'agent'; project: string; session: string; delegation: string }
  | { view: 'tree'; project: string; session: string }
  | { view: 'files'; project: string; session: string; run: string }
  | { view: 'run'; project: string; session: string; run: string }
  | { view: 'finding'; project: string; session: string; finding: string }
  | {
      view: 'document';
      project: string;
      document: string;
      revision: number | null;
      block: string | null;
    }
  | { view: 'history'; project: string; document: string }
  | {
      view: 'pdf';
      project: string;
      artifact: string;
      revision: number;
      sha256: string;
      page: number | null;
    }
  | { view: 'memory'; project: string }
  | { view: 'skills'; project: string }
  | { view: 'mcp'; project: string }
  | { view: 'monitor'; project: string }
  | { view: 'documents'; project: string }
  | { view: 'artifacts'; project: string }
  | { view: 'settings'; project: string }
  | { view: 'message'; project: string; message: string }
  | { view: 'ledger'; project: string }
  | { view: 'issues'; project: string }
  | { view: 'entry'; project: string; entry: string; revision: number | null }
  | { view: 'categories'; project: string }
  | { view: 'frontier'; project: string; category: string }
  | { view: 'missing' };

/** A ledger entry named by its ID or its handle, as the server takes it. */
export const ENTRY_NAME = /^(?:entry-[0-9a-f]{24}|(?:I|C|R|A|D|T|REF)-[1-9][0-9]{0,8})$/;

/** A frontier category's name, as the server takes it. */
export const CATEGORY_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

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
  if (parts.length === 3 && parts[2] === 'memory') return { view: 'memory', project };
  if (parts.length === 3 && parts[2] === 'skills') return { view: 'skills', project };
  if (parts.length === 3 && parts[2] === 'mcp') return { view: 'mcp', project };
  if (parts.length === 3 && parts[2] === 'monitor') return { view: 'monitor', project };
  if (parts.length === 3 && parts[2] === 'documents') return { view: 'documents', project };
  if (parts.length === 3 && parts[2] === 'artifacts') return { view: 'artifacts', project };
  if (parts.length === 3 && parts[2] === 'settings') return { view: 'settings', project };
  if (parts.length === 4 && parts[2] === 'messages') {
    const message = parts[3]!;
    return MESSAGE_ID.test(message) ? { view: 'message', project, message } : { view: 'missing' };
  }
  if (parts[2] === 'ledger') return ledgerRoute(project, parts.slice(3));
  if (parts[2] === 'files' && parts.length === 5) {
    const [session, run] = [decoded(parts[3]!), decoded(parts[4]!)];
    if (session !== null && run !== null) return { view: 'files', project, session, run };
  }
  if (parts[2] === 'frontier' && parts.length === 3) return { view: 'categories', project };
  if (parts[2] === 'frontier' && parts.length === 4) {
    const category = parts[3]!;
    return CATEGORY_NAME.test(category)
      ? { view: 'frontier', project, category }
      : { view: 'missing' };
  }
  const name = parts.length >= 4 ? decoded(parts[3]!) : null;
  if (name === null) return { view: 'missing' };
  if (parts[2] === 'sessions' && parts.length === 4) {
    return { view: 'session', project, session: name };
  }
  if (parts[2] === 'sessions' && parts.length === 5 && parts[4] === 'tree') {
    return { view: 'tree', project, session: name };
  }
  if (parts[2] === 'sessions' && parts.length === 6 && parts[4] === 'runs') {
    const run = decoded(parts[5]!);
    if (run !== null) return { view: 'run', project, session: name, run };
  }
  if (parts[2] === 'sessions' && parts.length === 6 && parts[4] === 'reviews') {
    const review = decoded(parts[5]!);
    if (review !== null) return { view: 'review', project, session: name, review };
  }
  if (parts[2] === 'sessions' && parts.length === 6 && parts[4] === 'agents') {
    const delegation = decoded(parts[5]!);
    if (delegation !== null) return { view: 'agent', project, session: name, delegation };
  }
  if (parts[2] === 'sessions' && parts.length === 6 && parts[4] === 'findings') {
    const finding = decoded(parts[5]!);
    if (finding !== null) return { view: 'finding', project, session: name, finding };
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
  if (
    parts[2] === 'artifacts' &&
    parts[4] === 'revisions' &&
    (parts.length === 7 || (parts.length === 9 && parts[7] === 'pages'))
  ) {
    const number = revision(parts[5]);
    const sha256 = parts[6]!;
    const page = parts.length === 9 ? revision(parts[8]) : null;
    if (number !== null && /^[0-9a-f]{64}$/.test(sha256) && (parts.length === 7 || page !== null)) {
      return { view: 'pdf', project, artifact: name, revision: number, sha256, page };
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

export function mcpHref(project: string): string {
  return `${projectHref(project)}/mcp`;
}

/** The project's Monitor view (D103). */
export function monitorHref(project: string): string {
  return `${projectHref(project)}/monitor`;
}

/** The project's documents, beside the one chosen. */
export function documentsHref(project: string): string {
  return `${projectHref(project)}/documents`;
}

/** Every file the project's runs recorded, results first. */
export function artifactsHref(project: string): string {
  return `${projectHref(project)}/artifacts`;
}

export function settingsHref(project: string): string {
  return `${projectHref(project)}/settings`;
}

export function sessionHref(project: string, session: string): string {
  return `${projectHref(project)}/sessions/${encodeURIComponent(session)}`;
}

/** The project's artifact list, filtered to the files a session's run recorded (RA-37). */
export function filesHref(project: string, session: string, run: string): string {
  return `${projectHref(project)}/files/${encodeURIComponent(session)}/${encodeURIComponent(run)}`;
}

/** A session's run tree (PD-42). */
export function treeHref(project: string, session: string): string {
  return `${sessionHref(project, session)}/tree`;
}

/** One run's events, as it writes them, running or ended. */
export function runHref(project: string, session: string, run: string): string {
  return `${sessionHref(project, session)}/runs/${encodeURIComponent(run)}`;
}

/** A session with one of its consistency reviews chosen in its review panel. */
export function reviewHref(project: string, session: string, review: string): string {
  return `${sessionHref(project, session)}/reviews/${encodeURIComponent(review)}`;
}

/** A session with one delegation's card open in its Agents panel. */
export function agentHref(project: string, session: string, delegation: string): string {
  return `${sessionHref(project, session)}/agents/${encodeURIComponent(delegation)}`;
}

export function findingHref(project: string, session: string, finding: string): string {
  return `${sessionHref(project, session)}/findings/${encodeURIComponent(finding)}`;
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

/** A published PDF revision in the viewer; at `page`, opened at that page. */
export function pdfHref(
  project: string,
  artifact: string,
  revision: number,
  sha256: string,
  page?: number,
) {
  const base = `${projectHref(project)}/artifacts/${encodeURIComponent(artifact)}/revisions/${revision}/${sha256}`;
  return page === undefined ? base : `${base}/pages/${page}`;
}

/** An archived message's ID: `msg-` and 16 hexadecimal digits (CM-3). */
export const MESSAGE_ID = /^msg-[0-9a-f]{16}$/;

/** One archived message of the project, read whole. */
export function messageHref(project: string, message: string): string {
  return `${projectHref(project)}/messages/${encodeURIComponent(message)}`;
}

export function memoryHref(project: string): string {
  return `${projectHref(project)}/memory`;
}

export function skillsHref(project: string): string {
  return `${projectHref(project)}/skills`;
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

export function frontierHref(project: string): string {
  return `${projectHref(project)}/frontier`;
}

/** One frontier category's frontier. */
export function categoryHref(project: string, category: string): string {
  return `${frontierHref(project)}/${encodeURIComponent(category)}`;
}
