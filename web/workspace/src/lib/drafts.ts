// SPDX-License-Identifier: MIT
/** Drafts survive view changes in memory and reloads in this browser tab. */
export interface Draft {
  base: number;
  text: string;
  reason: string;
  block: string | null;
  retained: string;
}
const memory = new Map<string, Draft>();
const key = (project: string, document: string) => `document-draft:${project}:${document}`;

export function loadDraft(project: string, document: string): Draft | null {
  const id = key(project, document);
  const found = memory.get(id);
  if (found) return found;
  try {
    const raw = sessionStorage.getItem(id);
    if (!raw || raw.length > 3 * 1024 * 1024) return null;
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object') return null;
    const draft = value as Draft;
    if (
      !Number.isSafeInteger(draft.base) ||
      draft.base < 1 ||
      typeof draft.text !== 'string' ||
      typeof draft.reason !== 'string' ||
      typeof draft.retained !== 'string' ||
      !(draft.block === null || typeof draft.block === 'string')
    )
      return null;
    memory.set(id, draft);
    return draft;
  } catch {
    return null;
  }
}
export function saveDraft(project: string, document: string, draft: Draft): boolean {
  const id = key(project, document);
  memory.set(id, { ...draft });
  try {
    sessionStorage.setItem(id, JSON.stringify(draft));
    return true;
  } catch {
    return false;
  }
}
export function clearDraft(project: string, document: string): void {
  const id = key(project, document);
  memory.delete(id);
  try {
    sessionStorage.removeItem(id);
  } catch {
    /* Memory draft is already cleared. */
  }
}
