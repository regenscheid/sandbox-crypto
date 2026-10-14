<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, type CommentBodySource } from '../lib/api';
  import { documentRequest, documentAnswered } from '../lib/documentRequests';
  import { loadDraft, saveDraft, clearDraft } from '../lib/drafts';
  import ErrorMessage from './ErrorMessage.svelte';
  let {
    projectId,
    documentId,
    threadId,
    expected,
    source,
    changed,
  }: {
    projectId: string;
    documentId: string;
    threadId: string;
    expected: number;
    source: CommentBodySource;
    changed: () => Promise<void>;
  } = $props();
  const initial = untrack(() => source);
  const key = untrack(() => `${documentId}:comment-body:${initial.entry_id}`);
  const draft = untrack(() => loadDraft(projectId, key));
  function retained(value?: string | null): { base: number; sha256: string } {
    try {
      const parsed = JSON.parse(value ?? 'null');
      if (
        parsed &&
        Number.isInteger(parsed.base) &&
        parsed.base >= 0 &&
        /^[0-9a-f]{64}$/.test(parsed.sha256)
      )
        return parsed;
    } catch {
      /* A damaged optional draft does not grant a revision. */
    }
    return { base: initial.revision!, sha256: initial.sha256! };
  }
  let body = $state(draft?.text ?? initial.body ?? '');
  let threadBase = $state(draft?.base ?? untrack(() => expected));
  let bodyBase = $state(retained(draft?.retained));
  let editing = $state(Boolean(draft)),
    busy = $state(false),
    persisted = $state(true);
  let error = $state<unknown>(null);
  $effect(() => {
    if (editing)
      persisted = saveDraft(projectId, key, {
        text: body,
        base: threadBase,
        reason: 'edit_body',
        retained: JSON.stringify(bodyBase),
        block: null,
      });
  });
  async function save() {
    busy = true;
    error = null;
    try {
      const change = {
        action: 'edit_body',
        thread: threadId,
        entry: initial.entry_id,
        expected: threadBase,
        base: bodyBase.base,
        sha256: bodyBase.sha256,
        body,
      };
      const request = await documentRequest(projectId, documentId, change);
      await api.changeComment(projectId, documentId, { ...change, operation_id: request.id });
      documentAnswered(request);
      clearDraft(projectId, key);
      editing = false;
      await changed();
    } catch (caught) {
      error = caught;
    } finally {
      busy = false;
    }
  }
  function useCurrent() {
    threadBase = expected;
    bodyBase = { base: source.revision!, sha256: source.sha256! };
  }
  function open() {
    body = source.body ?? '';
    useCurrent();
    editing = true;
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (editing && !persisted) event.preventDefault();
  }}
/>
{#if source.revision !== undefined && source.sha256}
  {#if editing}
    <fieldset disabled={busy}>
      <p>
        Editing original entry <code>{initial.entry_id}</code>, body revision {bodyBase.base},
        thread revision {threadBase}. History and existing work requests retain their original text.
      </p>
      {#if threadBase !== expected || bodyBase.base !== source.revision || bodyBase.sha256 !== source.sha256}
        <p>
          Your body draft is retained. <button onclick={useCurrent}
            >Use current body and thread revisions</button
          >
        </p>
      {/if}
      <label
        >Comment body Markdown<textarea
          aria-label={`Edit comment body ${initial.entry_id}`}
          bind:value={body}
          maxlength="8192"
          rows="5"></textarea></label
      >
      <p>
        An empty body removes its formulas from the current body; the original and corrections
        remain in history.
      </p>
      <button onclick={save}>Save body correction</button><button
        onclick={() => {
          editing = false;
          clearDraft(projectId, key);
        }}>Discard body draft</button
      >
    </fieldset>
  {:else}<button onclick={open}>Edit this comment body</button>{/if}
{/if}
{#if !persisted}<p role="alert">
    Browser storage is unavailable. Keep this tab open or copy your body draft.
  </p>{/if}
{#if error}<ErrorMessage {error} />{/if}

<style>
  label {
    display: block;
  }
  textarea {
    width: 100%;
    box-sizing: border-box;
  }
  fieldset {
    border: 0;
    padding: 0;
  }
  button {
    margin: 0.3rem;
  }
</style>
