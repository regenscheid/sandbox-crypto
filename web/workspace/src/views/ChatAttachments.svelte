<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { uploadsApi, type UserUpload } from '../lib/uploads';
  import { uploadDraft, flushUploads } from '../lib/uploadDrafts.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  let {
    projectId,
    sessionId,
    disabled = false,
  }: { projectId: string; sessionId: string; disabled?: boolean } = $props();
  const draft = untrack(() => uploadDraft(projectId, sessionId));
  const files = $derived(draft.files),
    pending = $derived(draft.pending),
    busy = $derived(draft.busy),
    error = $derived(draft.error);
  let input: HTMLInputElement;
  let previous = $state<UserUpload[]>([]);
  let showPrevious = $state(false);
  let cursor = $state<number | null>(null);
  let loading = $state(false);
  async function add(selected: File[]) {
    if (disabled || busy) return;
    if (files.length + pending.length + selected.length > 8) {
      draft.error = new Error('Attach up to eight files per message.');
      return;
    }
    draft.pending = [
      ...pending,
      ...selected.map((file) => ({ file, id: `upload-${crypto.randomUUID()}` })),
    ];
    await retry();
  }
  function retry() {
    return flushUploads(projectId, sessionId);
  }
  async function readPrevious(more = false) {
    loading = true;
    draft.error = null;
    try {
      const page = await uploadsApi.list(projectId, sessionId, more ? (cursor ?? 0) : 0);
      previous = more ? [...previous, ...page.uploads] : page.uploads;
      cursor = page.next_after;
      showPrevious = true;
    } catch (caught) {
      draft.error = caught;
    } finally {
      loading = false;
    }
  }
</script>

<!-- This labelled region accepts file drops as well as an ordinary keyboard file picker. -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="chat-attachments"
  ondragover={(event) => {
    if (!disabled) event.preventDefault();
  }}
  ondrop={(event) => {
    event.preventDefault();
    void add([...(event.dataTransfer?.files ?? [])]);
  }}
>
  <input
    bind:this={input}
    class="sr-only"
    type="file"
    multiple
    aria-label="Upload files"
    disabled={disabled || busy}
    onchange={() => {
      const chosen = [...(input.files ?? [])];
      input.value = '';
      void add(chosen);
    }}
  />
  <div class="attachment-actions">
    <button type="button" disabled={disabled || busy} onclick={() => input.click()}
      >＋ Attach files</button
    ><button
      type="button"
      disabled={disabled || loading}
      onclick={() => (showPrevious ? (showPrevious = false) : void readPrevious())}
      >Stored files</button
    ><span class="muted">or drop here · up to 100 MiB each</span>
  </div>
  {#if files.length || pending.length}<ul class="attachment-cards" aria-label="Message attachments">
      {#each files as file (file.upload_id)}<li>
          <span
            ><strong>{file.filename}</strong><small
              >Stored · {(file.size / 1024).toFixed(1)} KiB</small
            ></span
          ><button
            type="button"
            disabled={disabled || busy}
            aria-label={`Remove ${file.filename} from message`}
            onclick={() => {
              draft.files = files.filter((f) => f.upload_id !== file.upload_id);
            }}>×</button
          >
        </li>{/each}
      {#each pending as item (item.id)}<li>
          <span>{item.file.name}<small>{busy ? 'Uploading…' : 'Not uploaded'}</small></span
          >{#if !busy}<button
              aria-label={`Remove ${item.file.name}`}
              onclick={() => {
                draft.pending = pending.filter((p) => p.id !== item.id);
              }}>×</button
            >{/if}
        </li>{/each}
    </ul>{/if}
  {#if pending.length && !busy}<button {disabled} onclick={retry}>Retry upload</button>{/if}
  {#if showPrevious}<div class="stored-uploads">
      <button
        class="quiet-button"
        onclick={() => {
          showPrevious = false;
        }}>Close stored files</button
      >
      {#each previous as file (file.upload_id)}<button
          disabled={disabled ||
            busy ||
            files.length + pending.length >= 8 ||
            files.some((f) => f.upload_id === file.upload_id)}
          onclick={() => {
            draft.files = [...files, file];
          }}>{file.filename}</button
        >{:else}<p class="muted">No stored files yet.</p>{/each}
      {#if cursor !== null}<button disabled={loading} onclick={() => readPrevious(true)}
          >More files</button
        >{/if}
    </div>{/if}
  {#if error}<ErrorMessage {error} />{/if}
</div>
