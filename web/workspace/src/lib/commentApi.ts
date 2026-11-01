// SPDX-License-Identifier: MIT
/** The workspace comment client, through the shared session transport. */
import type {
  CommentListing,
  CommentThread,
  CommentChange,
  CommentChangeRequest,
} from './api-types';
import { get, post } from './http';

const part = encodeURIComponent;

export const commentApi = {
  comments: (project: string, document: string, revision: number, after: string | null = null) =>
    get<CommentListing>(
      `/projects/${part(project)}/documents/${part(document)}/comments?revision=${revision}` +
        (after === null ? '' : `&after=${part(after)}`),
    ),
  commentThread: (
    project: string,
    document: string,
    thread: string,
    before: number | null = null,
  ) =>
    get<CommentThread>(
      `/projects/${part(project)}/documents/${part(document)}/comments/${part(thread)}` +
        (before === null ? '' : `?before=${before}`),
    ),
  changeComment: (project: string, document: string, change: CommentChangeRequest) =>
    post<CommentChange>(`/projects/${part(project)}/documents/${part(document)}/comments`, change),
};
