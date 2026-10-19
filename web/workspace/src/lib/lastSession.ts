// SPDX-License-Identifier: MIT
/** Last visited session, for an explicit return link from project browsers. */
const key = (project: string) => `sandbox-science:last-session:${project}`;

export function rememberSession(project: string, session: string): void {
  try {
    sessionStorage.setItem(key(project), session);
  } catch {
    // Private browsing can disable storage; normal sidebar navigation still works.
  }
}

export function lastSession(project: string): string | null {
  try {
    return sessionStorage.getItem(key(project));
  } catch {
    return null;
  }
}
