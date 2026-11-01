// SPDX-License-Identifier: MIT
/**
 * The editors' drafts, one typed store per kind (mutation.ts). Each draft keeps the
 * revision it was based on, so a save can be checked against the current revision.
 */
import { DraftStore } from './mutation';

type Check = (value: unknown) => boolean;
const text: Check = (value) => typeof value === 'string';
const revision: Check = (value) => Number.isSafeInteger(value) && (value as number) >= 0;
const nullableText: Check = (value) => value === null || typeof value === 'string';
const oneOf =
  (...values: string[]): Check =>
  (value) =>
    typeof value === 'string' && values.includes(value);

function shape<T>(fields: Record<keyof T & string, Check>) {
  return (value: unknown): value is T => {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
    const found = value as Record<string, unknown>;
    const keys = Object.keys(fields);
    return (
      Object.keys(found).length === keys.length &&
      keys.every((key) => fields[key as keyof T & string](found[key]))
    );
  };
}

/** A document edit: the whole source (`block` null) or one block's Markdown. */
export interface DocumentDraft {
  base: number;
  text: string;
  reason: string;
  block: string | null;
  /** The user's earlier text, kept visible while a conflict is resolved. */
  retained: string;
}
export const documentDrafts = new DraftStore<DocumentDraft>(
  'document',
  shape<DocumentDraft>({
    base: revision,
    text,
    reason: text,
    block: nullableText,
    retained: text,
  }),
  3 * 1024 * 1024,
);

/** A new comment and its anchor. */
export interface CommentDraft {
  base: number;
  block: string;
  body: string;
  kind: string;
  math: string;
  quote: string;
  session: string;
}
export const commentDrafts = new DraftStore<CommentDraft>(
  'comment',
  shape<CommentDraft>({
    base: revision,
    block: text,
    body: text,
    kind: text,
    math: text,
    quote: text,
    session: text,
  }),
);

/** A reply to a comment thread, or an edit of its latest entry. */
export interface ReplyDraft {
  expected: number;
  body: string;
  mode: 'reply' | 'edit';
  session: string;
}
export const replyDrafts = new DraftStore<ReplyDraft>(
  'reply',
  shape<ReplyDraft>({
    expected: revision,
    body: text,
    mode: oneOf('reply', 'edit'),
    session: text,
  }),
);

/** A correction of a comment's original body, against its thread and body revisions. */
export interface BodyCorrectionDraft {
  expected: number;
  body: string;
  base: number;
  sha256: string;
}
export const bodyCorrectionDrafts = new DraftStore<BodyCorrectionDraft>(
  'comment-body',
  shape<BodyCorrectionDraft>({
    expected: revision,
    body: text,
    base: revision,
    sha256: (value) => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value),
  }),
);

/** A user's response to a review finding. */
export interface FindingDraft {
  expected: number;
  explanation: string;
  action: string;
  references: string;
  /** For setting `superseded`: the finding that replaces it, which the server checks. */
  successor?: string;
}
const findingShape = {
  expected: revision,
  explanation: text,
  action: text,
  references: text,
};
const withSuccessor = shape<Required<FindingDraft>>({ ...findingShape, successor: text });
const withoutSuccessor = shape<Omit<FindingDraft, 'successor'>>(findingShape);
// A draft kept before the successor field existed still loads.
export const findingDrafts = new DraftStore<FindingDraft>(
  'finding',
  (value: unknown): value is FindingDraft => withSuccessor(value) || withoutSuccessor(value),
);

/** The project's macro setting, as JSON text. */
export interface MacroDraft {
  expected: number;
  text: string;
}
export const macroDrafts = new DraftStore<MacroDraft>(
  'macros',
  shape<MacroDraft>({ expected: revision, text }),
);

/** A chat message to a session's main agent, not yet sent (at most 64 KiB, RS-4). */
export interface ChatDraft {
  text: string;
}
export const chatDrafts = new DraftStore<ChatDraft>('chat', shape<ChatDraft>({ text }));
