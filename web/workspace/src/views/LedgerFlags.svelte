<!-- SPDX-License-Identifier: MIT -->
<!-- An entry's open flags for reconsideration (docs/specs/ledger.md, LG-28, LG-29): each
     flag's cause, the entry it came from and the rest of what it says, and the user's
     clearing of them, one, those selected or all, always with a reason. A flag changes
     nothing in the entry; clearing it says the entry still holds or is not affected. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { EntryView } from '../lib/api-types';
  import {
    CAUSE_WORDS,
    changeFlags,
    flagDetails,
    flagSource,
    MAX_REASON,
    openFlagsText,
    reasonProblem,
  } from '../lib/ledgerFlags';
  import { idle } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    entry,
    changed,
  }: { projectId: string; entry: EntryView; changed: () => Promise<void> } = $props();
  let selected = $state<Record<string, boolean>>({});
  let reason = $state('');
  let problem = $state<string | null>(null);
  const saving = $state(idle());
  const chosen = $derived(
    entry.flags.filter((flag) => selected[flag.flag_id]).map((f) => f.flag_id),
  );

  async function clear(flags: string[] | null): Promise<void> {
    problem = reasonProblem(reason);
    if (problem !== null) return;
    const change = flags === null ? { reason } : { flags, reason };
    const done = await changeFlags(
      saving,
      'clear',
      projectId,
      entry.entry_id,
      change,
      (body) => api.clearLedgerFlags(projectId, entry.entry_id, body),
      changed,
    );
    if (done) {
      reason = '';
      selected = {};
    }
  }
</script>

{#if entry.open_flags > 0}
  <h2>Flags <span class="muted">({openFlagsText(entry.open_flags)})</span></h2>
  <p class="muted">
    A flag asks for the entry to be reconsidered after something it rests on changed. It changes
    nothing in the entry. Clear it once the entry has been checked: it still holds, or the change
    does not affect it.
  </p>
  <ul class="flags">
    {#each entry.flags as flag (flag.flag_id)}
      {@const source = flagSource(projectId, flag.source)}
      <li>
        <label>
          <input type="checkbox" bind:checked={selected[flag.flag_id]} disabled={saving.busy} />
          <strong>{CAUSE_WORDS[flag.cause]}</strong>:
        </label>
        {#if source.href}<a href={source.href}>{source.text}</a>{:else}{source.text}{/if}
        <span class="muted">({flagDetails(flag)}; {flag.flag_id})</span>
        <button type="button" disabled={saving.busy} onclick={() => clear([flag.flag_id])}
          >Clear this flag</button
        >
      </li>
    {/each}
  </ul>
  {#if entry.open_flags > entry.flags.length}
    <p class="muted">
      The newest {entry.flags.length} are listed; "Clear all" clears every one of the {entry.open_flags}.
    </p>
  {/if}
  <label class="reason"
    >Reason for clearing (required)
    <textarea bind:value={reason} maxlength={MAX_REASON} rows="3" disabled={saving.busy}
    ></textarea></label
  >
  <p>
    <button
      type="button"
      disabled={saving.busy || chosen.length === 0}
      onclick={() => clear(chosen)}>Clear the {chosen.length} selected</button
    >
    <button type="button" disabled={saving.busy} onclick={() => clear(null)}
      >Clear all {entry.open_flags} open flags</button
    >
  </p>
  {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
  {#if saving.error}<ErrorMessage error={saving.error} />{/if}
{/if}
