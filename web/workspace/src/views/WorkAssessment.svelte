<!-- SPDX-License-Identifier: MIT -->
<!-- The user's acceptance or rejection of an obstruction claimed on an issue (docs/specs/
     ledger.md, LG-7): opening it takes a copy of the issue as shown and of the claim; the
     panel shows the claim, what was tried and its evidence, and the issue's statement and
     fields as exact text (ApprovalText), since an acceptance holds only for that wording;
     it takes the user's note, what they read, and records the assessment at the revision
     read. A newer revision is a conflict, which records nothing and says so; when the issue
     shown moves on, the panel says so and offers no confirmation. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { EntryView, WorkRecord } from '../lib/api-types';
  import { fieldsText } from '../lib/ledgerCheck';
  import {
    assessmentConflict,
    assessmentProblem,
    claimText,
    MAX_ASSESSMENT_NOTE,
    type Assessing,
  } from '../lib/ledgerWork';
  import { guardedWrite } from '../lib/mutation';
  import { idle, submit } from '../lib/submit';
  import ApprovalText from './ApprovalText.svelte';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    entry,
    claim,
    verdict,
    done,
    oncancel,
  }: {
    projectId: string;
    /** The issue as shown now; the panel keeps the copy it opened on. */
    entry: EntryView;
    claim: WorkRecord;
    verdict: Assessing;
    /** After the assessment, or a failure: what to say, and the entry read again. */
    done: (text: string | null) => Promise<void>;
    oncancel: () => void;
  } = $props();
  // The panel is opened on one issue revision and one claim, and closed after.
  const reading = untrack(() => $state.snapshot(entry)) as EntryView;
  const claimed = untrack(() => $state.snapshot(claim)) as WorkRecord;
  const accepting = untrack(() => verdict === 'accepted');
  let note = $state('');
  let problem = $state<string | null>(null);
  const saving = $state(idle());
  const moved = $derived(entry.revision !== reading.revision);
  const conflict = $derived(assessmentConflict(saving.error, reading.handle, reading.revision));
  const fields = fieldsText(reading.fields);

  async function confirm(): Promise<void> {
    if (moved) return;
    problem = assessmentProblem(note);
    if (problem !== null) return;
    const body = {
      claim: claimed.record_id,
      verdict: accepting ? ('accepted' as const) : ('rejected' as const),
      revision: reading.revision,
      note,
    };
    const ok = await submit(
      saving,
      async () => {
        await guardedWrite('ledger-assessment', `${projectId}:${reading.entry_id}`, body, (sent) =>
          api.assessLedgerEntry(projectId, reading.entry_id, sent),
        );
      },
      () => done(null),
    );
    if (ok) {
      await done(
        `Your ${accepting ? 'acceptance' : 'rejection'} of the obstruction claimed in ` +
          `${claimed.record_id}, on ${reading.handle} r${reading.revision}, is recorded.`,
      );
    }
  }
</script>

<div class="frontier-action">
  <p>
    You {accepting ? 'accept' : 'reject'} the obstruction claimed in work record
    <code>{claimed.record_id}</code>, on {reading.handle} as r{reading.revision} reads.
    {#if accepting}
      The main agent may then obstruct the issue by a decision that names your acceptance, and only
      while its statement and fields read as below: a reworded issue needs a fresh acceptance.
    {/if}
    Read what you decide on:
  </p>
  {#if moved}
    <p class="problem" role="alert">
      {reading.handle} has moved on to r{entry.revision} since you opened this. What is below is r{reading.revision},
      which can no longer be assessed: cancel, read the issue as it is now, then assess it again.
    </p>
  {/if}
  <ApprovalText
    text={claimText(claimed)}
    label="The claim: what was tried, and its evidence, exactly"
  />
  <ApprovalText
    text={reading.statement}
    preview
    label={`The statement of ${reading.handle} r${reading.revision}, exactly`}
  />
  {#if fields}
    <ApprovalText text={fields} label="Its fields, exactly" />
  {:else}
    <p class="muted">It has no fields of its own.</p>
  {/if}
  <label class="reason"
    >Your note: what you read, and why you {accepting ? 'accept' : 'reject'} the claim
    <textarea bind:value={note} maxlength={MAX_ASSESSMENT_NOTE} rows="3" disabled={saving.busy}
    ></textarea></label
  >
  <p>
    <button type="button" disabled={saving.busy || moved || conflict !== null} onclick={confirm}
      >{accepting ? 'Accept' : 'Reject'} the claim at r{reading.revision}</button
    >
    <button type="button" disabled={saving.busy} onclick={oncancel}>Cancel</button>
  </p>
  {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
  {#if conflict}
    <p class="problem" role="alert">{conflict}</p>
  {:else if saving.error}
    <ErrorMessage error={saving.error} />
  {/if}
</div>
