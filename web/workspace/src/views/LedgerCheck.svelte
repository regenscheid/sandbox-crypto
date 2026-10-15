<!-- SPDX-License-Identifier: MIT -->
<!-- "I checked this revision" (docs/specs/ledger.md, LG-16, LG-17): on a claim, result or
     reference at its current revision, the user records that they checked it themselves;
     a reference's check vouches for the PDF that revision names. Opening the panel
     takes a copy of the revision shown; the panel shows that copy, everything the check
     pins, as exact text (ApprovalText), and the check names its revision, so a newer one is
     a conflict. When the entry shown moves on, the panel says so and offers no
     confirmation: the user never confirms text they did not read. -->
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
  /** A copy of the entry as it was when the panel was opened; null when it is closed. */
  let reading = $state<EntryView | null>(null);
  let note = $state('');
  let problem = $state<string | null>(null);
  let recorded = $state<number | null>(null);
  const saving = $state(idle());

  /** The entry shown has moved on from the copy the panel shows. */
  const moved = $derived(reading !== null && entry.revision !== reading.revision);

  async function confirm(): Promise<void> {
    if (reading === null || moved) return;
    problem = noteProblem(note);
    if (problem !== null) return;
    const revision = reading.revision;
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
          reading = $state.snapshot(entry);
          recorded = null;
          problem = null;
        }}>I checked this revision…</button
      >
    </p>
    {#if recorded !== null}<p role="status">
        Your check of r{recorded} is recorded.{entry.type === 'reference'
          ? ' It vouches for the PDF that revision names.'
          : ''}
      </p>{/if}
    {#if checkFailure(saving.error)}
      <p class="problem" role="alert">{checkFailure(saving.error)}</p>
    {:else if saving.error}
      <ErrorMessage error={saving.error} />
    {/if}
  {:else}
    <div class="check">
      {#if reading.type === 'reference'}
        <p>
          You record that you checked revision {reading.revision} of {reading.handle} yourself: its citation,
          and the PDF it names, by the PDF's SHA-256. Your check vouches for that PDF, so the consistency
          reviewer's check of a quote from it can confirm the results that quote it. Read what you attest
          to:
        </p>
      {:else}
        <p>
          You record that you checked revision {reading.revision} of {reading.handle} yourself: an independent
          check of yours, which the gate counts when the main agent establishes it, as long as its statement,
          fields, assumptions and the evidence that bears on it stay as they are. Read what you attest
          to:
        </p>
      {/if}
      {#if moved}
        <p class="problem" role="alert">
          The entry has moved on to r{entry.revision} since you opened this. What is below is r{reading.revision},
          which can no longer be checked: cancel, read the entry as it is now, then check it again.
        </p>
      {/if}
      <LedgerCheckText entry={reading} />
      <label class="reason"
        >A note (optional): what you checked, and how
        <textarea bind:value={note} maxlength={MAX_NOTE} rows="3" disabled={saving.busy}
        ></textarea></label
      >
      <p>
        <button type="button" disabled={saving.busy || moved} onclick={confirm}
          >Record my check of r{reading.revision}</button
        >
        <button type="button" disabled={saving.busy} onclick={() => (reading = null)}>Cancel</button
        >
      </p>
      {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
    </div>
  {/if}
{/if}
