// SPDX-License-Identifier: MIT
/** Keep an uncertain document mutation retry under the same server operation ID. */
export interface DocumentRequest { key: string; id: string }
const pending = new Map<string, string>();

export async function documentRequest(project: string, document: string, body: Record<string, unknown>): Promise<DocumentRequest> {
  const bytes = new TextEncoder().encode(JSON.stringify([project, document, body]));
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  const key = `document-request:${Array.from(digest, byte => byte.toString(16).padStart(2, '0')).join('')}`;
  let id = pending.get(key);
  if (!id) {
    try { id = sessionStorage.getItem(key) ?? undefined; } catch { /* Memory covers view changes. */ }
    if (!id || !/^[0-9a-f-]{36}$/.test(id)) id = crypto.randomUUID();
    pending.set(key, id);
    try { sessionStorage.setItem(key, id); } catch { /* Draft UI warns if storage fails. */ }
  }
  return { key, id };
}

/** Call only once a definitive server result was parsed, never on a transport error. */
export function documentAnswered(request: DocumentRequest): void {
  pending.delete(request.key);
  try { sessionStorage.removeItem(request.key); } catch { /* Memory request is cleared. */ }
}
