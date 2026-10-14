// SPDX-License-Identifier: MIT
/**
 * The one save pattern of the workspace's editors: a typed draft that survives view
 * changes and reloads in this tab, an operation ID kept until the server's answer is
 * known, and the server's revision check, whose refusal is a conflict.
 *
 * - `DraftStore<T>` keeps one kind of draft, by scope, in memory and in sessionStorage,
 *   checking each stored value against that kind's own validator.
 * - `guardedWrite` sends a change with an operation ID. The ID depends on the change's
 *   kind, scope and body, and is kept until a result: after any failure, sending the
 *   same change again reuses it, so the server replays the first answer instead of
 *   applying the change twice. A conflict or refusal answered again is answered the
 *   same way; a changed draft is a new body and gets a new ID.
 */

/** What a stored draft looked like when it was read. */
export interface LoadedDraft<T> {
  draft: T | null;
  /** A stored value was there but unreadable or invalid; it is left as it is. */
  damaged: boolean;
  /** The draft would survive a reload: it is in sessionStorage, not only in memory. */
  stored: boolean;
}

const FORMAT = 1;
const drafts = new Map<string, { draft: unknown; stored: boolean }>();
const copy = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

export class DraftStore<T> {
  constructor(
    readonly kind: string,
    private readonly valid: (value: unknown) => value is T,
    private readonly maxBytes = 512 * 1024,
  ) {}

  private key(scope: string): string {
    return `draft:${this.kind}:${scope}`;
  }

  load(scope: string): LoadedDraft<T> {
    const key = this.key(scope);
    const kept = drafts.get(key);
    if (kept) return { draft: copy(kept.draft as T), damaged: false, stored: kept.stored };
    try {
      const raw = sessionStorage.getItem(key);
      if (raw === null) return { draft: null, damaged: false, stored: true };
      if (raw.length > this.maxBytes) return { draft: null, damaged: true, stored: false };
      const stored: unknown = JSON.parse(raw);
      const value =
        typeof stored === 'object' && stored !== null ? (stored as Record<string, unknown>) : null;
      if (value?.format !== FORMAT || value.scope !== scope || !this.valid(value.draft))
        return { draft: null, damaged: true, stored: false };
      drafts.set(key, { draft: copy(value.draft), stored: true });
      return { draft: copy(value.draft), damaged: false, stored: true };
    } catch {
      return { draft: null, damaged: true, stored: false };
    }
  }

  /**
   * Keep `draft`; false when it is invalid or could not be stored for a reload, in which
   * case it is kept in memory only and any older stored copy is removed.
   */
  save(scope: string, draft: T): boolean {
    if (!this.valid(draft)) return false;
    const key = this.key(scope);
    drafts.set(key, { draft: copy(draft), stored: false });
    try {
      const encoded = JSON.stringify({ format: FORMAT, scope, draft });
      if (new TextEncoder().encode(encoded).length <= this.maxBytes) {
        sessionStorage.setItem(key, encoded);
        drafts.set(key, { draft: copy(draft), stored: true });
        return true;
      }
    } catch {
      /* Removed below. */
    }
    // An older stored copy must not come back as the current draft after a reload.
    try {
      sessionStorage.removeItem(key);
    } catch {
      /* Nothing more can be done; the memory copy is current. */
    }
    return false;
  }

  /** False when the stored copy could not be removed. */
  clear(scope: string): boolean {
    const key = this.key(scope);
    drafts.delete(key);
    try {
      sessionStorage.removeItem(key);
      return true;
    } catch {
      return false;
    }
  }
}

const operations = new Map<string, string>();

async function operationKey(kind: string, scope: string, body: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify([kind, scope, body]));
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  return `operation:${Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
}

function forget(key: string): void {
  operations.delete(key);
  try {
    sessionStorage.removeItem(key);
  } catch {
    /* The memory copy is gone. */
  }
}

/**
 * Forget the ID kept for a change that failed and was then given up (its draft changed),
 * so that the same change written later is a new one, not a retry of the old.
 */
export async function forgetWrite(kind: string, scope: string, body: unknown): Promise<void> {
  forget(await operationKey(kind, scope, body));
}

/**
 * Send `body` with an operation ID through `send`; resolves with the answer or throws
 * its `ApiError`. The ID is kept until a result, so a retry of the same change reuses it.
 */
export async function guardedWrite<B extends object, R>(
  kind: string,
  scope: string,
  body: B,
  send: (change: B & { operation_id: string }) => Promise<R>,
): Promise<R> {
  const key = await operationKey(kind, scope, body);
  let id = operations.get(key);
  if (!id) {
    try {
      id = sessionStorage.getItem(key) ?? undefined;
    } catch {
      /* Memory covers view changes. */
    }
    if (!id || !/^[0-9a-f-]{36}$/.test(id)) id = crypto.randomUUID();
    operations.set(key, id);
    try {
      sessionStorage.setItem(key, id);
    } catch {
      /* The draft's own warning says the tab cannot keep it. */
    }
  }
  // Only a result forgets the ID. After any failure, even a refusal or a sign-out, the
  // earlier attempt may have committed; the same change sent again must replay it.
  const answer = await send({ ...body, operation_id: id });
  forget(key);
  return answer;
}
