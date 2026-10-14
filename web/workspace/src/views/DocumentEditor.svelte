<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, type DocumentRevision, type DocumentChange } from '../lib/api';
  import { describeAuthor } from '../lib/authors';
  import { documentDrafts } from '../lib/drafts';
  import { guardedWrite } from '../lib/mutation';
  import { documentHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  let {
    projectId,
    found,
    refreshed,
  }: { projectId: string; found: DocumentRevision; refreshed: () => void } = $props();
  const id = $derived(found.document.document_id);
  const scope = $derived(`${projectId}:${id}`);
  const initial = untrack(() => found);
  const restored = untrack(
    () => documentDrafts.load(`${projectId}:${initial.document.document_id}`).draft,
  );
  let open = $state(restored !== null);
  let base = $state(restored?.base ?? initial.revision.number);
  let text = $state(restored?.text ?? initial.text);
  let reason = $state(restored?.reason ?? 'User edit');
  let block = $state<string | null>(restored?.block ?? null);
  let retained = $state(restored?.retained ?? '');
  let selected = $state(initial.blocks[0]?.id ?? '');
  let busy = $state(false);
  let error = $state<unknown>(null);
  let result = $state<DocumentChange | null>(null);
  let current = $state<DocumentRevision | null>(null);
  let persisted = $state(true);
  let projectName = $state(initial.document.name);
  $effect(() => {
    if (open) persisted = documentDrafts.save(scope, { base, text, reason, block, retained });
  });
  function begin(selectedBlock: string | null) {
    block = selectedBlock;
    text =
      selectedBlock === null
        ? found.text
        : (found.blocks.find((item) => item.id === selectedBlock)?.source ?? '');
    base = found.revision.number;
    result = null;
    error = null;
    open = true;
  }
  async function save() {
    busy = true;
    error = null;
    try {
      const body = {
        action: block === null ? 'source' : 'patch',
        base,
        reason,
        ...(block === null ? { text } : { operations: [{ op: 'replace', block, markdown: text }] }),
      };
      result = await guardedWrite('document', scope, body, (change) =>
        api.changeDocument(projectId, id, change),
      );
      if (['applied', 'unchanged'].includes(result.outcome)) {
        documentDrafts.clear(scope);
        open = false;
        refreshed();
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
    retained = text;
    base = current.revision.number;
    // Source resolution starts with the current document so untouched newer blocks
    // cannot be overwritten by the old full-source draft. The old draft stays visible.
    if (block === null || !current.blocks.some((item) => item.id === block)) {
      block = null;
      text = current.text;
    }
    result = null;
  }
  async function action(action: 'promote' | 'revert') {
    busy = true;
    error = null;
    try {
      const body = {
        action,
        base: found.document.head,
        reason:
          action === 'promote'
            ? 'Move workbook document to project'
            : `Restore revision ${found.revision.number}`,
        ...(action === 'promote' ? { name: projectName } : { target: found.revision.number }),
      };
      await guardedWrite('document', scope, body, (change) =>
        api.changeDocument(projectId, id, change),
      );
      location.hash = documentHref(projectId, id);
      refreshed();
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
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (open && !persisted) event.preventDefault();
  }}
/>
<section class="editor">
  {#if !open}
    {#if !found.revision.tombstone && found.matched && found.revision.number === found.document.head}
      <button onclick={() => begin(null)}>Edit Markdown source</button>
      <label
        >Block <select bind:value={selected}
          >{#each found.blocks as item (item.id)}<option value={item.id}
              >{item.id} · {item.kind}</option
            >{/each}</select
        ></label
      >
      <button disabled={!selected} onclick={() => begin(selected)}>Edit selected block</button>
      {#if found.document.scope === 'session'}
        <label>Project document name <input bind:value={projectName} maxlength="64" /></label>
        <button disabled={busy} onclick={() => action('promote')}>Move to project</button>
      {/if}
    {:else if !found.revision.tombstone && found.revision.number < found.document.head}
      <p>
        Restoring this revision appends its text and original block IDs after revision {found
          .document.head}.
      </p>
      <button disabled={busy} onclick={() => action('revert')}>Restore this revision</button>
    {/if}
  {:else}
    <h2>{block === null ? 'Edit Markdown source' : `Edit block ${block}`}</h2>
    <p>
      Based on revision {base}. Your draft is retained in this browser tab until saved or discarded.
    </p>
    {#if !persisted}<p role="alert">
        Browser storage is unavailable. Keep this tab open or copy your draft before closing it.
      </p>{/if}
    <label
      >Markdown<textarea
        aria-label="Markdown draft"
        bind:value={text}
        disabled={busy}
        rows="16"
        spellcheck="false"></textarea></label
    >
    <label>Reason <input bind:value={reason} disabled={busy} maxlength="1000" /></label>
    <button disabled={busy || !reason.trim()} onclick={save}
      >Save {block === null ? 'source' : 'block'}</button
    >
    <button disabled={busy} onclick={discard}>Discard draft</button>
    {#if result}
      <p role="alert">{result.message || result.outcome} Your draft is still above.</p>
      {#each result.conflicts as conflict, index (index)}
        <h3>Current {conflict.block ?? conflict.math}</h3>
        <pre>{conflict.now ?? 'Removed'}</pre>
        {#each conflict.changed_by ?? [] as change (change.revision)}<p>
            Revision {change.revision} by {describeAuthor(change.author)}
          </p>{/each}
      {/each}
      {#if current}<details>
          <summary>Current revision {current.revision.number}</summary>
          <pre>{current.text}</pre>
        </details>{/if}
      <button disabled={busy} onclick={resolve}
        >{block === null
          ? 'Start resolution from current revision'
          : 'Use current revision as base for this block'}</button
      >
    {/if}
    {#if retained}<details open>
        <summary>Retained draft before resolution — copy the changes you want</summary><textarea
          aria-label="Retained draft"
          readonly
          value={retained}
          rows="10"></textarea>
      </details>{/if}
  {/if}
  {#if error}<ErrorMessage {error} />{/if}
</section>

<style>
  .editor {
    margin: 1.5rem 0;
    padding: 1rem;
    border: 1px solid var(--border, #9996);
    border-radius: 0.4rem;
  }
  label {
    display: block;
    margin: 0.6rem 0;
  }
  textarea {
    display: block;
    width: 100%;
    font-family: monospace;
    box-sizing: border-box;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  button {
    margin: 0.25rem 0.5rem 0.25rem 0;
  }
</style>
