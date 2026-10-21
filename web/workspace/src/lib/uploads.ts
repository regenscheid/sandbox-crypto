// SPDX-License-Identifier: MIT
/** Original upload metadata only; file bytes never enter a browser draft store. */
import { get, post, request } from './http';
import { DraftStore } from './mutation';

export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;
export type { UserUpload, UploadPage } from './api-types';
import type { UserUpload, UploadPage } from './api-types';
const at = (project: string, session: string) =>
  `/projects/${encodeURIComponent(project)}/sessions/${encodeURIComponent(session)}/uploads`;
export const uploadsApi = {
  list: (project: string, session: string, after = 0, signal?: AbortSignal) =>
    get<UploadPage>(`${at(project, session)}?after=${after}`, { signal, deadlineMs: 10000 }),
  download: (file: UserUpload) =>
    `/api/v0${at(file.project, file.session)}/${encodeURIComponent(file.upload_id)}/content`,
  document: (file: UserUpload) =>
    post<{ document_id: string }>(
      `${at(file.project, file.session)}/${encodeURIComponent(file.upload_id)}/document`,
      {},
    ),
};
export const attachmentDrafts = new DraftStore<UserUpload[]>(
  'chat-attachments',
  (value): value is UserUpload[] =>
    Array.isArray(value) &&
    value.length <= 8 &&
    value.every((file: unknown) => {
      if (typeof file !== 'object' || file === null) return false;
      const f = file as Record<string, unknown>;
      return (
        [
          'upload_id',
          'project',
          'session',
          'filename',
          'media_type',
          'sha256',
          'created_at',
          'author',
          'path',
        ].every((k) => typeof f[k] === 'string') &&
        typeof f.size === 'number' &&
        typeof f.number === 'number'
      );
    }),
  16384,
);

export async function uploadFile(
  project: string,
  session: string,
  id: string,
  file: File,
): Promise<UserUpload> {
  if (file.size > MAX_UPLOAD_BYTES)
    throw new Error(`${file.name} exceeds the 100 MiB upload limit.`);
  return request<UserUpload>(at(project, session), {
    method: 'POST',
    binaryBody: file,
    uploadMetadata: encodeURIComponent(
      JSON.stringify({
        upload_id: id,
        filename: file.name,
        media_type: file.type || 'application/octet-stream',
      }),
    ),
    deadlineMs: 300_000,
    maxBytes: 16_384,
  });
}

const MANIFEST_HEAD = '\n\nAttached files (sandbox copies; original bytes retained):\n```json\n';

/** A file a message names in its manifest, as `attachmentMessage` writes it. */
export interface AttachedFile {
  filename: string;
  path: string;
  size: number;
  sha256: string;
}

/**
 * A message's own words and the files its manifest lists, so the conversation shows the
 * files as files rather than as JSON. A text without a manifest that reads back, such as
 * one the user wrote that only looks like one, stays whole.
 */
export function splitAttachments(text: string): { text: string; files: AttachedFile[] } {
  const start = text.lastIndexOf(MANIFEST_HEAD);
  if (start < 0 || !text.endsWith('\n```')) return { text, files: [] };
  let listed: unknown;
  try {
    listed = JSON.parse(text.slice(start + MANIFEST_HEAD.length, -4));
  } catch {
    return { text, files: [] };
  }
  if (!Array.isArray(listed) || listed.length === 0) return { text, files: [] };
  const files: AttachedFile[] = [];
  for (const item of listed) {
    const file = (item ?? {}) as Record<string, unknown>;
    if (typeof file.filename !== 'string' || typeof file.size !== 'number')
      return { text, files: [] };
    files.push({
      filename: file.filename,
      path: typeof file.path === 'string' ? file.path : '',
      size: file.size,
      sha256: typeof file.sha256 === 'string' ? file.sha256 : '',
    });
  }
  return { text: text.slice(0, start), files };
}

/** Matches the server's manifest exactly for message and question-answer budgets. */
export function attachmentMessage(text: string, files: UserUpload[]): string {
  if (!files.length) return text;
  const manifest = files.map((f) => ({
    upload_id: f.upload_id,
    filename: f.filename,
    path: f.path,
    sha256: f.sha256,
    size: f.size,
  }));
  return text.trimEnd() + MANIFEST_HEAD + JSON.stringify(manifest, null, 2) + '\n```';
}
