// SPDX-License-Identifier: MIT
/** The workspace document client, through the shared session transport. */
import type {
  DocumentListing,
  DocumentRevision,
  DocumentChange,
  DocumentChangeRequest,
  PatchPreview,
  DocumentHistory,
} from './api-types';
import { get, post } from './http';

const part = encodeURIComponent;

export const documentApi = {
  documents: (project: string, signal?: AbortSignal) =>
    get<DocumentListing>(`/projects/${part(project)}/documents`, { signal }).then(
      (found) => found.documents,
    ),
  document: (project: string, document: string, revision: number | null = null) =>
    get<DocumentRevision>(
      `/projects/${part(project)}/documents/${part(document)}` +
        (revision === null ? '' : `?revision=${revision}`),
    ),
  changeDocument: (project: string, document: string, change: DocumentChangeRequest) =>
    post<DocumentChange>(`/projects/${part(project)}/documents/${part(document)}/changes`, change),
  previewPatch: (project: string, document: string, patch: string) =>
    get<PatchPreview>(
      `/projects/${part(project)}/documents/${part(document)}/patches/${part(patch)}/preview`,
    ),
  history: (project: string, document: string) =>
    get<DocumentHistory>(`/projects/${part(project)}/documents/${part(document)}/history`),
};
