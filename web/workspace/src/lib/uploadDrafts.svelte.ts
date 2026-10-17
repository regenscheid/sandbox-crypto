// SPDX-License-Identifier: MIT
/** Session-owned transfer queues survive view changes; only metadata is persisted. */
import { attachmentDrafts, uploadFile, type UserUpload } from './uploads';
export interface UploadDraft {
  files: UserUpload[];
  pending: { file: File; id: string }[];
  busy: boolean;
  error: unknown;
}
const drafts = new Map<string, UploadDraft>();
export function uploadDraft(project: string, session: string): UploadDraft {
  const scope = `${project}:${session}`;
  const found = drafts.get(scope);
  if (found) return found;
  const created = $state<UploadDraft>({
    files: attachmentDrafts.load(scope).draft ?? [],
    pending: [],
    busy: false,
    error: null,
  });
  drafts.set(scope, created);
  return created;
}
export async function flushUploads(project: string, session: string): Promise<void> {
  const draft = uploadDraft(project, session);
  if (draft.busy) return;
  draft.busy = true;
  draft.error = null;
  try {
    while (draft.pending.length) {
      const item = draft.pending[0]!;
      const file = await uploadFile(project, session, item.id, item.file);
      draft.files = [...draft.files, file];
      draft.pending = draft.pending.slice(1);
      // Also persist while no view is mounted, before releasing the browser's File.
      attachmentDrafts.save(`${project}:${session}`, $state.snapshot(draft.files));
    }
  } catch (error) {
    draft.error = error;
  } finally {
    draft.busy = false;
  }
}

/** App-level protection also covers queues whose session view is no longer mounted. */
export function guardPendingUploads(event: BeforeUnloadEvent): void {
  if ([...drafts.values()].some((draft) => draft.pending.length)) event.preventDefault();
}
