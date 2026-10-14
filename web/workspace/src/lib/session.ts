// SPDX-License-Identifier: MIT
/**
 * The page's sign-out state. Signing out, or any request answered 401, ends the browser
 * session for the page's lifetime: every later request refuses at once, and listeners
 * (pollers, editors) stop. A new login needs a page reload.
 */
const listeners = new Set<() => void>();
let ended = false;

export function signedOut(): boolean {
  return ended;
}

/** Call `listener` once the session ends, at once if it has; returns the removal. */
export function onSignOut(listener: () => void): () => void {
  if (ended) {
    listener();
    return () => undefined;
  }
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function signOut(): void {
  if (ended) return;
  ended = true;
  for (const listener of [...listeners]) listener();
  listeners.clear();
}
