<!-- SPDX-License-Identifier: MIT -->
<!-- The user's dropping of a `dominates` link (docs/specs/ledger.md, LG-22): the panel reads
     the link's holder as it is now and shows the link's reason exactly (ApprovalText), the
     text the user acts on, then takes the user's own reason and drops the link at the
     revision read: a newer one is a conflict, which drops nothing and says so. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { DominatesLink, LedgerEntry } from '../lib/api-types';
  import { Detail, noDetail } from '../lib/detail';
  import { dropFailure, dropLink, dropProblem, heldLink } from '../lib/frontierLinks';
  import { MAX_REASON } from '../lib/ledgerFlags';
  import { idle } from '../lib/submit';
  import ApprovalText from './ApprovalText.svelte';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    link,
    done,
    oncancel,
  }: {
    projectId: string;
    link: DominatesLink;
    /** After the drop: what to say, and the frontier read again. */
    done: (text: string) => Promise<void>;
    oncancel: () => void;
  } = $props();
  const read = $state(noDetail<string, LedgerEntry>());
  // The panel is made for one link and closed after it: the holder is read once.
  const holder = new Detail(read, (entry: string) => api.ledgerEntry(projectId, entry));
  void holder.show(untrack(() => link.entry_id));
  const entry = $derived(read.value?.entry ?? null);
  const held = $derived(entry === null ? null : heldLink(entry, link.target));
  let reason = $state('');
  let problem = $state<string | null>(null);
  const saving = $state(idle());

  async function drop(): Promise<void> {
    if (entry === null || held === null) return;
    problem = dropProblem(reason);
    if (problem !== null) return;
    const revision = entry.revision;
    const name = entry.handle;
    await dropLink(
      saving,
      projectId,
      entry.entry_id,
      { target: held.target_id, revision, reason },
      (body) => api.dropDominatesLink(projectId, entry.entry_id, body),
      () => done(`Your drop of ${name}'s link to ${link.target}, at r${revision}, is recorded.`),
    );
  }
</script>

<div class="frontier-action">
  {#if read.loading}<p class="muted">Reading {link.handle} as it is now…</p>{/if}
  {#if read.error}<ErrorMessage error={read.error} />{/if}
  {#if entry !== null && held === null}
    <p role="alert" class="problem">
      {entry.handle} r{entry.revision} no longer holds a dominates link to {link.target}.
    </p>
  {:else if entry !== null && held !== null}
    <p>
      You drop the link by which {entry.handle} dominates {link.target}, as {entry.handle} r{entry.revision}
      holds it. Read the reason it gives, exactly as recorded:
    </p>
    {#if held.note === null}
      <p class="muted">It gives no reason.</p>
    {:else}
      <ApprovalText text={held.note} label="The link's reason" />
    {/if}
    <label class="reason"
      >Your reason for dropping it
      <textarea bind:value={reason} maxlength={MAX_REASON} rows="3" disabled={saving.busy}
      ></textarea></label
    >
    <p>
      <button type="button" disabled={saving.busy} onclick={drop}
        >Drop the link at r{entry.revision}</button
      >
      <button type="button" disabled={saving.busy} onclick={oncancel}>Cancel</button>
    </p>
    {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
    {#if dropFailure(saving.error, entry.handle, entry.revision)}
      <p class="problem" role="alert">{dropFailure(saving.error, entry.handle, entry.revision)}</p>
    {:else if saving.error}
      <ErrorMessage error={saving.error} />
    {/if}
  {/if}
  {#if entry === null || held === null}
    <p><button type="button" onclick={oncancel}>Close</button></p>
  {/if}
</div>
