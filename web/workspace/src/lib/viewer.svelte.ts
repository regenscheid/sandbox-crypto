// SPDX-License-Identifier: MIT
/**
 * Who the page's session is (D104), from `GET /api/v0/me`: the name the sidebar shows,
 * and whether the admin-only actions are this user's. Those are accepting a classification
 * draft and re-accepting after drift (CL-3, CL-4), a project grant, clearing a hold and
 * turning an MCP service back on. Their buttons stay on the page, disabled with the
 * reason, for a user who is not an admin; so do a resource extension and saving the
 * agents' models. The server decides every action again; this
 * only shows what it would refuse. Until the answer arrives nothing is disabled.
 */
import type { Viewer } from './api-types';

export const viewer = $state<{ current: Viewer | null }>({ current: null });

export function setViewer(found: Viewer | null): void {
  viewer.current = found;
}

/** Why the admin-only actions are not this user's; null when they are. */
export function adminBlock(): string | null {
  const found = viewer.current;
  if (found === null || found.admin) return null;
  return found.admin_group
    ? `Only members of the Authentik group ${found.admin_group} can do this.`
    : 'Only the workspace’s admins can do this.';
}

/** The name the page shows for the session. */
export function viewerName(found: Viewer): string {
  return found.login === 'token' ? 'operator (login link)' : found.user;
}
