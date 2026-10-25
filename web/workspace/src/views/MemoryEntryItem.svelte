<!-- SPDX-License-Identifier: MIT -->
<!-- One accepted entry of the project memory (docs/specs/project-memory.md, PM-4): its
     exact text as every agent reads it (ApprovalText, with a rendered preview), whether
     agents receive it, and the user's
     changes, each naming the revision shown. Its history is one click away. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { memoryEdits } from '../lib/browserState';
  import type { MemoryEntry } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { anchorText, basisText, type MemoryAction, textProblem } from '../lib/memory';
  import { visible } from '../lib/text';
  import ApprovalText from './ApprovalText.svelte';
  import MemoryHistory from './MemoryHistory.svelte';

  let {
    projectId,
    entry,
    first,
    last,
    given,
    busy,
    onchange,
  }: {
    projectId: string;
    entry: MemoryEntry;
    first: boolean;
    last: boolean;
    /** Whether agents receive this entry, within the bound (PM-7). */
    given: boolean;
    busy: boolean;
    onchange: (change: MemoryAction) => Promise<boolean>;
  } = $props();
  /** The text being edited, and the revision it was opened at; null when not editing. */
  const scope = untrack(() => `${projectId}:${entry.entry_id}`);
  const restored = untrack(() => memoryEdits.load(scope));
  const kept = restored.draft;
  let stored = $state(restored.stored);
  let draft = $state<string | null>(kept?.text ?? null);
  let base = $state(kept?.base ?? 0);
  $effect(() => {
    if (draft !== null) stored = memoryEdits.save(scope, { text: draft, base });
  });
  let history = $state(false);
  const problem = $derived(draft === null ? null : textProblem(draft));

  function edit() {
    draft = entry.text;
    base = entry.revision;
  }
  async function save() {
    if (draft === null || problem !== null) return;
    const change = { action: 'edit' as const, entry: entry.entry_id, expected: base, text: draft };
    if (await onchange(change)) cancel();
  }
  function cancel() {
    memoryEdits.clear(scope);
    draft = null;
  }
  function move(direction: 'up' | 'down') {
    void onchange({ action: 'move', entry: entry.entry_id, expected: entry.revision, direction });
  }
  function remove() {
    if (window.confirm(`Remove ${entry.entry_id}? Its history is kept.`)) {
      void onchange({ action: 'remove', entry: entry.entry_id, expected: entry.revision });
    }
  }
</script>

<li class="entry" class:left={!given}>
  {#if restored.damaged}
    <p role="alert">
      A saved memory edit could not be restored. Check your text before continuing.
    </p>
  {/if}
  <p class="head muted">
    <code>{entry.entry_id}</code> · revision {entry.revision} · {formatTime(entry.changed_at)}
    {#if !given}<strong>not given to agents: past the bound</strong>{/if}
  </p>
  <p class="head muted">
    Basis: <strong>{basisText(entry.basis, entry.accepted)}</strong>{#if entry.accepted}
      (an agent's proposal, accepted){/if}
    · recorded {formatTime(entry.recorded_at)} · scope {entry.scope}
    {#if entry.anchors.length > 0}
      · anchors:
      {#each entry.anchors as anchor, index (index)}<code>{visible(anchorText(anchor))}</code
        >{' '}{/each}
    {/if}
  </p>
  {#if draft === null}
    <ApprovalText text={entry.text} preview label="Exactly as every agent reads it" />
    <button type="button" disabled={busy} onclick={edit}>Edit</button>
    <button type="button" disabled={busy || first} onclick={() => move('up')}>Move up</button>
    <button type="button" disabled={busy || last} onclick={() => move('down')}>Move down</button>
    <button type="button" disabled={busy} onclick={remove}>Remove</button>
  {:else}
    <ApprovalText text={draft} before={entry.text} label="Exact proposed edit" />
    <textarea aria-label={`The text of ${entry.entry_id}`} rows="5" bind:value={draft}></textarea>
    {#if !stored}
      <p role="alert">
        This memory edit is kept only in this tab. Copy it before reloading or closing the tab.
      </p>
    {/if}
    {#if problem}<p role="alert">{problem}</p>{/if}
    {#if base !== entry.revision}
      <div role="note">
        <p>
          {entry.entry_id} changed while you edited it: it is now at revision {entry.revision}, and
          your text is based on revision {base}. Its text now:
        </p>
        <ApprovalText text={entry.text} label="Its text now" />
        <button type="button" onclick={() => (base = entry.revision)}
          >Keep my text, over revision {entry.revision}</button
        >
      </div>
    {/if}
    <button type="button" disabled={busy || problem !== null} onclick={save}>Save</button>
    <button type="button" onclick={cancel}>Cancel</button>
  {/if}
  <button type="button" onclick={() => (history = !history)}
    >{history ? 'Hide the history' : 'History'}</button
  >
  {#if history}<MemoryHistory {projectId} entry={entry.entry_id} />{/if}
</li>

<style>
  .entry {
    overflow-wrap: anywhere;
  }
  .head {
    margin: 0 0 8px;
    font-size: 12px;
  }
  .entry > button {
    margin: 8px 6px 0 0;
  }
  .left {
    opacity: 0.7;
  }
  textarea {
    width: 100%;
    box-sizing: border-box;
  }
</style>
