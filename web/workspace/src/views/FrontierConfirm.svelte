<!-- SPDX-License-Identifier: MIT -->
<!-- The user's confirmation of an unverified result on the frontier (docs/specs/ledger.md,
     LG-16 and LG-20 as amended, LG-17): a published result an agent recorded counts once
     the user checked it. The panel reads the result as it is now and shows everything the
     check pins as exact text (ApprovalText), then records the user's check of that
     revision: a newer one is a conflict, which records nothing, says so and offers the check
     no more: the user cancels and confirms again, reading it as it is now. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, ApiError } from '../lib/api';
  import type { FrontierResult, LedgerEntry } from '../lib/api-types';
  import { Detail, noDetail } from '../lib/detail';
  import {
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
    result,
    done,
    oncancel,
  }: {
    projectId: string;
    result: FrontierResult;
    /** After the check: what to say, and the frontier read again. */
    done: (text: string) => Promise<void>;
    oncancel: () => void;
  } = $props();
  const read = $state(noDetail<string, LedgerEntry>());
  // The panel is made for one result and closed after it: the result is read once.
  const reader = new Detail(read, (entry: string) => api.ledgerEntry(projectId, entry));
  void reader.show(untrack(() => result.entry_id));
  const entry = $derived(read.value?.entry ?? null);
  let note = $state('');
  let problem = $state<string | null>(null);
  const saving = $state(idle());
  /** The result has a newer revision than the one read: no check of it is offered again. */
  const conflict = $derived(saving.error instanceof ApiError && saving.error.status === 409);

  async function confirm(): Promise<void> {
    if (entry === null) return;
    problem = noteProblem(note);
    if (problem !== null) return;
    const { entry_id, handle, revision } = entry;
    const recorded = await recordCheck(
      saving,
      projectId,
      entry_id,
      checkRecord(revision, note),
      (body) => api.checkLedgerEntry(projectId, entry_id, body),
      async () => {},
    );
    if (recorded) await done(`Your check of ${handle} r${revision} is recorded.`);
  }
</script>

<div class="frontier-action">
  {#if read.loading}<p class="muted">Reading {result.handle} as it is now…</p>{/if}
  {#if read.error}<ErrorMessage error={read.error} />{/if}
  {#if entry !== null}
    <p>
      You confirm {entry.handle} by recording that you checked revision {entry.revision} yourself: it
      counts on the frontier as long as its statement, fields, assumptions and evidence stay as they are.
      Read what you attest to:
    </p>
    {#if entry.revision !== result.revision}
      <p class="problem" role="alert">
        The frontier shows r{result.revision}, and {entry.handle} is now at r{entry.revision}: what
        you check is r{entry.revision}, below.
      </p>
    {/if}
    <LedgerCheckText {entry} />
    <label class="reason"
      >A note (optional): what you checked, and how
      <textarea bind:value={note} maxlength={MAX_NOTE} rows="3" disabled={saving.busy}
      ></textarea></label
    >
    <p>
      <button type="button" disabled={saving.busy || conflict} onclick={confirm}
        >Record my check of r{entry.revision}</button
      >
      <button type="button" disabled={saving.busy} onclick={oncancel}>Cancel</button>
    </p>
    {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
    {#if conflict}
      <p class="problem" role="alert">
        {entry.handle} has a newer revision than the r{entry.revision} you read, so nothing was recorded.
        Cancel, then confirm it again to read it as it is now.
      </p>
    {:else if saving.error}
      <ErrorMessage error={saving.error} />
    {/if}
  {:else if !read.loading}
    <p><button type="button" onclick={oncancel}>Close</button></p>
  {/if}
</div>
