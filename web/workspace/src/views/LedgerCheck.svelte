<!-- SPDX-License-Identifier: MIT -->
<!-- "I checked this revision" (docs/specs/ledger.md, LG-17): on a claim or result at its
     current revision, the user records that they checked it themselves. Before they
     confirm, the revision's title, statement and fields show as exact text (ApprovalText);
     the check names that revision, so a newer one is a conflict, and the panel closes when
     the entry shown moves on, so the user never confirms text they did not read. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { EntryView } from '../lib/api-types';
  import {
    checkable,
    checkFailure,
    checkRecord,
    MAX_NOTE,
    noteProblem,
    recordCheck,
  } from '../lib/ledgerCheck';
  import { idle } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  import LedgerCheckText from './LedgerCheckText.svelte';

  let {
    projectId,
    entry,
    changed,
  }: { projectId: string; entry: EntryView; changed: () => Promise<void> } = $props();
  /** The revision the panel was opened on; null when it is closed. */
  let reading = $state<number | null>(null);
  let note = $state('');
  let problem = $state<string | null>(null);
  let recorded = $state<number | null>(null);
  const saving = $state(idle());

  // The entry was read again and moved on: what the panel showed is no longer it.
  $effect(() => {
    if (reading !== null && entry.revision !== reading) reading = null;
  });

  async function confirm(): Promise<void> {
    if (reading === null) return;
    problem = noteProblem(note);
    if (problem !== null) return;
    const revision = reading;
    const done = await recordCheck(
      saving,
      projectId,
      entry.entry_id,
      checkRecord(revision, note),
      (body) => api.checkLedgerEntry(projectId, entry.entry_id, body),
      changed,
    );
    if (done) {
      recorded = revision;
      note = '';
    }
    reading = null;
  }
</script>

{#if checkable(entry)}
  <h2>Your check</h2>
  {#if reading === null}
    <p>
      <button
        type="button"
        disabled={saving.busy}
        onclick={() => {
          reading = entry.revision;
          recorded = null;
          problem = null;
        }}>I checked this revision…</button
      >
    </p>
    {#if recorded !== null}<p role="status">Your check of r{recorded} is recorded.</p>{/if}
    {#if checkFailure(saving.error)}
      <p class="problem" role="alert">{checkFailure(saving.error)}</p>
    {:else if saving.error}
      <ErrorMessage error={saving.error} />
    {/if}
  {:else}
    <div class="check">
      <p>
        You record that you checked revision {reading} of {entry.handle} yourself: an independent check
        of yours, which the gate counts when the main agent establishes it, as long as its statement,
        fields, assumptions and the evidence that bears on it stay as they are. Read what you attest to:
      </p>
      <LedgerCheckText {entry} />
      <label class="reason"
        >A note (optional): what you checked, and how
        <textarea bind:value={note} maxlength={MAX_NOTE} rows="3" disabled={saving.busy}
        ></textarea></label
      >
      <p>
        <button type="button" disabled={saving.busy} onclick={confirm}
          >Record my check of r{reading}</button
        >
        <button type="button" disabled={saving.busy} onclick={() => (reading = null)}>Cancel</button
        >
      </p>
      {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
    </div>
  {/if}
{/if}
