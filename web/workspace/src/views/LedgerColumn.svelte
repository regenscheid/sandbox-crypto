<!-- SPDX-License-Identifier: MIT -->
<!-- One column of the board: the entries of one type in one status, a page at a time. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { EntrySummary } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { matches, statusText, type EntryType } from '../lib/ledger';
  import { emptyList, PagedList } from '../lib/pagedList';
  import { ledgerEntryHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    type,
    status,
    filter,
  }: { projectId: string; type: EntryType; status: string; filter: string } = $props();
  const entries = $state(emptyList<EntrySummary, string>());
  // The board keys each column by its type and status, so these never change under it.
  const list = new PagedList(entries, async (after) => {
    const found = await api.ledger(projectId, type, status, after);
    return { items: found.page.entries, next: found.page.next_after };
  });
  void list.reload();
  const shown = $derived(entries.items.filter((entry) => matches(entry, filter)));
</script>

<section class="board-column" aria-label={statusText(status)}>
  <h3>
    {statusText(status)}
    <span class="muted">{entries.items.length}{entries.next ? '+' : ''}</span>
  </h3>
  {#each shown as entry (entry.entry_id)}
    <article class="card">
      <a href={ledgerEntryHref(projectId, entry.handle)}>{entry.handle}</a>
      {#if entry.origin === 'published'}<span class="muted">published</span>{/if}
      <p>{entry.title}</p>
      <p class="muted">r{entry.revision} · {formatTime(entry.updated)}</p>
    </article>
  {/each}
  {#if entries.loading}<p class="muted">Loading…</p>{/if}
  {#if entries.error}<ErrorMessage error={entries.error} />{/if}
  {#if entries.next}
    <button type="button" disabled={entries.loading} onclick={() => list.more()}>More</button>
  {/if}
</section>
