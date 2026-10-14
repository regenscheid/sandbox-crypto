<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, type CommentBodySource } from '../lib/api';
  import { bodyCorrection, correctionDraft, correctionOutdated } from '../lib/commentThreads';
  import { bodyCorrectionDrafts } from '../lib/drafts';
  import { guardedWrite } from '../lib/mutation';
  import { idle, submit } from '../lib/submit';
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
  const key = untrack(() => `${projectId}:${documentId}:${initial.entry_id}`);
  const stored = bodyCorrectionDrafts.load(key).draft;
  let draft = $state(stored ?? untrack(() => correctionDraft(initial, expected)));
  let editing = $state(Boolean(stored)),
    persisted = $state(true);
  const saving = $state(idle());
  $effect(() => {
    if (editing) persisted = bodyCorrectionDrafts.save(key, $state.snapshot(draft));
  });
  async function save() {
    await submit(saving, async () => {
      await guardedWrite(
        'comment',
        `${projectId}:${documentId}`,
        bodyCorrection(threadId, initial.entry_id, draft),
        (sent) => api.changeComment(projectId, documentId, sent),
      );
      bodyCorrectionDrafts.clear(key);
      editing = false;
      await changed();
    });
  }
  function useCurrent() {
    draft = { ...correctionDraft(source, expected), body: draft.body };
  }
  function open() {
    draft = correctionDraft(source, expected);
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
    <fieldset disabled={saving.busy}>
      <p>
        Editing original entry <code>{initial.entry_id}</code>, body revision {draft.base}, thread
        revision {draft.expected}. History and existing work requests retain their original text.
      </p>
      {#if correctionOutdated(draft, source, expected)}
        <p>
          Your body draft is retained. <button onclick={useCurrent}
            >Use current body and thread revisions</button
          >
        </p>
      {/if}
      <label
        >Comment body Markdown<textarea
          aria-label={`Edit comment body ${initial.entry_id}`}
          bind:value={draft.body}
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
          bodyCorrectionDrafts.clear(key);
        }}>Discard body draft</button
      >
    </fieldset>
  {:else}<button onclick={open}>Edit this comment body</button>{/if}
{/if}
{#if !persisted}<p role="alert">
    Browser storage is unavailable. Keep this tab open or copy your body draft.
  </p>{/if}
{#if saving.error}<ErrorMessage error={saving.error} />{/if}

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
