<!-- SPDX-License-Identifier: MIT -->
<!-- The project's issues (docs/specs/ledger.md, LG-34): those in one status, a page at a
     time, each opening to show what it concerns, what answers it and what it was split
     into. Flags and work records join the list when the ledger records them. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { EntrySummary } from '../lib/api-types';
  import { STATUSES, statusText } from '../lib/ledger';
  import { emptyList, PagedList } from '../lib/pagedList';
  import { ledgerHref, projectHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import IssueRow from './IssueRow.svelte';

  let { projectId }: { projectId: string } = $props();
  let status = $state('open');
  const issues = $state(emptyList<EntrySummary, string>());
  const list = new PagedList(issues, async (after) => {
    const found = await api.ledger(projectId, 'issue', status, after);
    return { items: found.page.entries, next: found.page.next_after };
  });
  $effect(() => {
    void status;
    // Another status is another list: the one shown goes, and its cursor, even if the
    // read of the new one fails.
    void list.restart();
  });
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
  <a href={ledgerHref(projectId)}>Ledger</a> /
</nav>
<h1>Issues</h1>
<label
  >Status <select bind:value={status}>
    {#each STATUSES.issue as option (option)}<option value={option}>{statusText(option)}</option
      >{/each}
  </select></label
>

{#if !issues.items.length && !issues.loading && !issues.error}
  <p>No {statusText(status)} issues.</p>
{/if}
<ul class="issues">
  {#each issues.items as issue (issue.entry_id)}
    <IssueRow {projectId} {issue} />
  {/each}
</ul>
{#if issues.loading}<p class="muted">Loading…</p>{/if}
{#if issues.error}<ErrorMessage error={issues.error} />{/if}
{#if issues.next}
  <button type="button" disabled={issues.loading} onclick={() => list.more()}>More</button>
{/if}
