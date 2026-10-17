// SPDX-License-Identifier: MIT
/** Original upload metadata only; file bytes never enter a browser draft store. */
import { get, post } from './http';
import { DraftStore } from './mutation';

export const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;
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
  if (file.size > MAX_UPLOAD_BYTES) throw new Error(`${file.name} exceeds the 8 MiB upload limit.`);
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 8192)
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  return post<UserUpload>(
    at(project, session),
    {
      upload_id: id,
      filename: file.name,
      media_type: file.type || 'application/octet-stream',
      content_b64: btoa(binary),
    },
    { deadlineMs: 60_000 },
  );
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
  return (
    text.trimEnd() +
    '\n\nAttached files (sandbox copies; original bytes retained):\n```json\n' +
    JSON.stringify(manifest, null, 2) +
    '\n```'
  );
}
