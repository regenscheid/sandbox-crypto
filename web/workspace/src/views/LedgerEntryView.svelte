<!-- SPDX-License-Identifier: MIT -->
<!-- One ledger entry at a revision, read by its handle or ID, and its history
     (docs/specs/ledger.md, LG-34). After the user's change to it, it is read again: the
     entry shown stays until the new read arrives. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { LedgerEntry } from '../lib/api-types';
  import { Detail, noDetail } from '../lib/detail';
  import { ledgerHref, projectHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import LedgerEntryBody from './LedgerEntryBody.svelte';
  import LedgerGate from './LedgerGate.svelte';
  import LedgerHistory from './LedgerHistory.svelte';
  import LedgerWork from './LedgerWork.svelte';
  import { holdsWork } from '../lib/ledgerWork';

  let {
    projectId,
    entryName,
    revision,
  }: { projectId: string; entryName: string; revision: number | null } = $props();
  const shown = $state(noDetail<string, LedgerEntry>());
  // The route keys this view by the entry and revision: they never change here.
  const detail = new Detail(shown, () => api.ledgerEntry(projectId, entryName, revision));
  void detail.show(untrack(() => `${entryName}@${revision ?? 'current'}`));
  /** Counts the user's changes, so the history reads its first page again after one. */
  let changes = $state(0);

  async function changed(): Promise<void> {
    await detail.reload();
    changes += 1;
  }
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
  <a href={ledgerHref(projectId)}>Ledger</a> /
</nav>

{#if shown.value}
  {@const entry = shown.value.entry}
  <LedgerEntryBody {projectId} {entry} {changed} />
  <LedgerGate {projectId} entryId={entry.entry_id} shown={entry.revision} {changes} />
  {#if holdsWork(entry.type)}<LedgerWork {projectId} {entry} {changes} {changed} />{/if}
  <LedgerHistory
    {projectId}
    entryId={entry.entry_id}
    handle={entry.handle}
    shown={entry.revision}
    {changes}
    {changed}
    open={entry.flags}
  />
{:else}
  <h1>{entryName}</h1>
  {#if shown.loading}<p class="muted">Loading…</p>{/if}
{/if}
{#if shown.error}<ErrorMessage error={shown.error} />{/if}
