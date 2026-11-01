<!-- SPDX-License-Identifier: MIT -->
<!--
  Editing a document: its Markdown beside a live preview, the whole source or one block, with
  the reason and Save in a bar below. It also asks the user to confirm a move into the
  project or a restore, with the exact text either adopts. It stays mounted while hidden,
  so a draft outlives switching back to reading.
-->
<script lang="ts" module>
  /** What the page asks the editor for: an edit, a move into the project, or a restore. */
  export type EditRequest =
    | { kind: 'source' }
    | { kind: 'block'; block: string }
    | { kind: 'promote' }
    | { kind: 'revert' };
</script>

<script lang="ts">
  import { untrack } from 'svelte';
  import { api, type DocumentRevision, type DocumentChange } from '../lib/api';
  import { describeAuthor } from '../lib/authors';
  import {
    documentAction,
    draftChange,
    resolveAgainst,
    saved,
    startDraft,
  } from '../lib/documentEdit';
  import { documentDrafts } from '../lib/drafts';
  import { macroSet } from '../lib/macros';
  import Markdown from '../lib/Markdown.svelte';
  import { guardedWrite } from '../lib/mutation';
  import { documentHref } from '../lib/router';
  import { visible } from '../lib/text';
  import ConfirmText from './ConfirmText.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  import HiddenBanner from './HiddenBanner.svelte';
  let {
    projectId,
    found,
    refreshed,
    request = null,
    consumeRequest = () => {},
    close = () => {},
  }: {
    projectId: string;
    found: DocumentRevision;
    refreshed: () => void;
    request?: EditRequest | null;
    consumeRequest?: () => void;
    /** Back to reading: after a save, a discard or a cancelled confirmation. */
    close?: () => void;
  } = $props();
  const id = $derived(found.document.document_id);
  const scope = $derived(`${projectId}:${id}`);
  const initial = untrack(() => found);
  const restored = untrack(
    () => documentDrafts.load(`${projectId}:${initial.document.document_id}`).draft,
  );
  let open = $state(restored !== null);
  let base = $state(restored?.base ?? initial.revision.number);
  let text = $state(restored?.text ?? initial.text);
  let reason = $state(restored?.reason ?? '');
  let block = $state<string | null>(restored?.block ?? null);
  let retained = $state(restored?.retained ?? '');
  /** The pane shown where the editor is too narrow for both side by side. */
  let pane = $state<'write' | 'preview'>('write');
  let busy = $state(false);
  let error = $state<unknown>(null);
  let result = $state<DocumentChange | null>(null);
  let current = $state<DocumentRevision | null>(null);
  let persisted = $state(true);
  let projectName = $state(initial.document.name);
  let field: HTMLTextAreaElement | undefined = $state();
  /** The move or restore the user is asked to confirm, with the text it adopts shown. */
  let confirming = $state<{ action: 'promote' | 'revert'; before: string | null } | null>(null);
  const editable = $derived(
    !found.revision.tombstone && found.matched && found.revision.number === found.document.head,
  );
  const macros = $derived(
    macroSet(found.macros ?? {}, `${projectId}:${found.macro_context?.key ?? 'legacy-document'}`),
  );
  const editedBlock = $derived(found.blocks.find((item) => item.id === block) ?? null);
  // The preview follows the draft after a pause in typing, not on every key.
  let preview = $state(untrack(() => text));
  $effect(() => {
    const next = text;
    const timer = setTimeout(() => (preview = next), 250);
    return () => clearTimeout(timer);
  });
  $effect(() => {
    const asked = request;
    if (!asked) return;
    untrack(() => {
      consumeRequest();
      if (asked.kind === 'promote' || asked.kind === 'revert') void ask(asked.kind);
      // A draft in progress is kept: the request shows it instead of starting another.
      else if (!open && editable) begin(asked.kind === 'block' ? asked.block : null);
    });
  });
  async function ask(action: 'promote' | 'revert') {
    error = null;
    if (action === 'promote') {
      confirming = { action, before: null };
      return;
    }
    try {
      // Restoring replaces the current text: show the differences from it.
      const before = (await api.document(projectId, id)).text;
      // Not if the editor opened while the current text was read.
      if (!open) confirming = { action, before };
    } catch (caught) {
      error = caught;
    }
  }
  $effect(() => {
    if (open) persisted = documentDrafts.save(scope, { base, text, reason, block, retained });
  });
  function begin(selectedBlock: string | null) {
    // The whole new draft: an earlier resolution's retained text does not carry over.
    ({ base, text, reason, block, retained } = startDraft(found, selectedBlock, ''));
    preview = text;
    current = null;
    result = null;
    error = null;
    // Editing replaces a move or restore the user was asked to confirm.
    confirming = null;
    open = true;
    pane = 'write';
    queueMicrotask(() => field?.focus());
  }
  async function save() {
    busy = true;
    error = null;
    try {
      const body = draftChange({
        base,
        text,
        reason: reason.trim() || 'User edit',
        block,
        retained,
      });
      result = await guardedWrite('document', scope, body, (change) =>
        api.changeDocument(projectId, id, change),
      );
      if (saved(result)) {
        documentDrafts.clear(scope);
        open = false;
        refreshed();
        close();
      } else current = await api.document(projectId, id);
    } catch (caught) {
      error = caught;
    } finally {
      busy = false;
    }
  }
  async function resolve() {
    error = null;
    if (!current) {
      try {
        current = await api.document(projectId, id);
      } catch (caught) {
        error = caught;
        return;
      }
    }
    ({ base, text, block, retained } = resolveAgainst(
      { base, text, reason, block, retained },
      current,
    ));
    result = null;
  }
  async function action(action: 'promote' | 'revert') {
    busy = true;
    error = null;
    try {
      const body = documentAction(found, action, projectName);
      await guardedWrite('document', scope, body, (change) =>
        api.changeDocument(projectId, id, change),
      );
      confirming = null;
      location.hash = documentHref(projectId, id);
      refreshed();
      close();
    } catch (caught) {
      error = caught;
    } finally {
      busy = false;
    }
  }
  function discard() {
    documentDrafts.clear(scope);
    open = false;
    result = null;
    current = null;
    retained = '';
    close();
  }
  function wholeSource() {
    if (text !== (editedBlock?.source ?? '') && !confirm('Discard the changes to this block?'))
      return;
    begin(null);
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (open && !persisted) event.preventDefault();
  }}
/>
<section class="editor" aria-label="Edit the document">
  {#if open}
    <div class="editor-head">
      <div class="editor-title">
        <strong
          >{block === null
            ? 'Editing the whole document'
            : `Editing ${editedBlock ? `a ${editedBlock.kind}` : `block ${block}`}`}</strong
        >
        <span class="muted"
          >Based on revision {base}. The draft stays in this browser until you save or discard it.</span
        >
      </div>
      {#if block !== null}<button type="button" class="link-button" onclick={wholeSource}
          >Edit the whole document instead</button
        >{/if}
      <div class="segmented panes" role="tablist" aria-label="Editor view">
        <button
          type="button"
          role="tab"
          aria-selected={pane === 'write'}
          onclick={() => (pane = 'write')}>Write</button
        ><button
          type="button"
          role="tab"
          aria-selected={pane === 'preview'}
          onclick={() => (pane = 'preview')}>Preview</button
        >
      </div>
    </div>
    <div class="save-bar">
      <label class="reason"
        ><span class="sr-only">Reason</span><input
          bind:value={reason}
          disabled={busy}
          maxlength="1000"
          placeholder="What changed? (optional)"
        /></label
      >
      <button type="button" disabled={busy} onclick={discard}>Discard</button>
      <button type="button" class="primary-button" disabled={busy} onclick={save}
        >{busy ? 'Saving…' : 'Save'}</button
      >
    </div>
    {#if !persisted}<p role="alert">
        Browser storage is unavailable. Keep this tab open or copy your draft before closing it.
      </p>{/if}
    <div class="edit-panes show-{pane}">
      <label class="write-pane"
        ><span class="pane-label">Markdown</span><textarea
          aria-label="Markdown draft"
          bind:this={field}
          bind:value={text}
          disabled={busy}
          rows={block === null ? 24 : 10}
          spellcheck="false"></textarea></label
      >
      <div class="preview-pane">
        <span class="pane-label">Preview</span>
        <div class="preview-body document">
          {#if preview.trim()}<Markdown source={preview} {macros} />{:else}<p class="muted">
              Nothing to show yet.
            </p>{/if}
        </div>
      </div>
    </div>
    <HiddenBanner {text} />
    {#if result}
      <div class="conflict" role="alert">
        <p>{result.message || result.outcome} Your draft is still above.</p>
        {#each result.conflicts as conflict, index (index)}
          <h3>Current {conflict.block ?? conflict.math}</h3>
          <pre>{visible(conflict.now ?? 'Removed')}</pre>
          {#each conflict.changed_by ?? [] as change (change.revision)}<p>
              Revision {change.revision} by {describeAuthor(change.author)}
            </p>{/each}
        {/each}
        {#if current}<details>
            <summary>Current revision {current.revision.number}</summary>
            <pre>{visible(current.text)}</pre>
          </details>{/if}
        <button disabled={busy} onclick={resolve}
          >{block === null
            ? 'Start resolution from current revision'
            : 'Use current revision as base for this block'}</button
        >
      </div>
    {/if}
    {#if retained}<details open>
        <summary>Retained draft before resolution — copy the changes you want</summary><textarea
          aria-label="Retained draft"
          readonly
          value={retained}
          rows="10"></textarea><HiddenBanner text={retained} />
      </details>{/if}
  {:else if confirming}
    {#if confirming.action === 'promote'}
      <label class="project-name"
        >Name in the project <input bind:value={projectName} maxlength="64" /></label
      >
    {:else}
      <p>
        Restoring this revision appends its text and original block IDs after revision {found
          .document.head}.
      </p>
    {/if}
    <ConfirmText
      title={confirming.action === 'promote'
        ? 'Move this document into the project'
        : `Restore revision ${found.revision.number}`}
      text={found.text}
      before={confirming.before}
      label={confirming.action === 'promote'
        ? 'The text the project document will hold, exactly'
        : `The text revision ${found.revision.number} restores, exactly`}
      confirm={confirming.action === 'promote' ? 'Move to project' : 'Restore this revision'}
      {busy}
      onconfirm={() => void action(confirming!.action)}
      oncancel={() => {
        confirming = null;
        close();
      }}
    />
  {:else if !editable}
    <p class="muted">This revision cannot be edited: only the latest revision can.</p>
  {/if}
  {#if error}<ErrorMessage {error} />{/if}
</section>

<style>
  .editor {
    margin: 0;
    container-type: inline-size;
  }
  .editor-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 16px;
    margin: 4px 0 12px;
  }
  .editor-title {
    flex: 1 1 320px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .editor-title .muted {
    font-size: 12px;
  }
  .panes {
    display: none;
  }
  .edit-panes {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 16px;
    align-items: stretch;
  }
  .pane-label {
    display: block;
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
    margin-bottom: 6px;
  }
  .write-pane,
  .preview-pane {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  textarea {
    display: block;
    width: 100%;
    flex: 1 1 auto;
    min-height: 24rem;
    box-sizing: border-box;
    padding: 12px 14px;
    border: 1px solid var(--border);
    border-radius: 10px;
    font-family: ui-monospace, Menlo, monospace;
    font-size: 13px;
    line-height: 1.6;
    resize: vertical;
  }
  .preview-body {
    flex: 1 1 auto;
    min-height: 24rem;
    max-height: 70vh;
    overflow: auto;
    padding: 12px 16px;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: var(--panel);
  }
  .save-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin: 0 0 12px;
  }
  .reason {
    flex: 1 1 260px;
  }
  .reason input {
    width: 100%;
    box-sizing: border-box;
  }
  .conflict {
    margin-top: 12px;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 10px;
  }
  .project-name {
    display: block;
    margin: 0 0 12px;
  }
  .project-name input {
    margin-left: 8px;
    max-width: 100%;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  @container (max-width: 760px) {
    .panes {
      display: flex;
    }
    .edit-panes {
      grid-template-columns: minmax(0, 1fr);
    }
    .edit-panes.show-write .preview-pane,
    .edit-panes.show-preview .write-pane {
      display: none;
    }
  }
</style>
