<!-- SPDX-License-Identifier: MIT -->
<!-- One ledger entry at a revision, read by its handle or ID, and its history
     (docs/specs/ledger.md, LG-34). Read-only. -->
<script lang="ts">
  import { api } from '../lib/api';
  import { ledgerHref, projectHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import LedgerEntryBody from './LedgerEntryBody.svelte';
  import LedgerHistory from './LedgerHistory.svelte';

  let {
    projectId,
    entryName,
    revision,
  }: { projectId: string; entryName: string; revision: number | null } = $props();
  const loading = $derived(api.ledgerEntry(projectId, entryName, revision));
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
  <a href={ledgerHref(projectId)}>Ledger</a> /
</nav>

{#await loading}
  <h1>{entryName}</h1>
  <p class="muted">Loading…</p>
{:then answer}
  <LedgerEntryBody {projectId} entry={answer.entry} />
  <LedgerHistory
    {projectId}
    entryId={answer.entry.entry_id}
    handle={answer.entry.handle}
    shown={answer.entry.revision}
  />
{:catch error}
  <h1>{entryName}</h1>
  <ErrorMessage {error} />
{/await}
