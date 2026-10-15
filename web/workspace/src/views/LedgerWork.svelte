<!-- SPDX-License-Identifier: MIT -->
<!-- The work records on an issue, claim or result (docs/specs/ledger.md, LG-6), newest
     first, 20 at a time with older ones on request; after the user's change to the entry,
     the first page is read again. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { EntryView, WorkRecord } from '../lib/api-types';
  import { WORK_PAGE } from '../lib/ledgerWork';
  import { emptyList, PagedList } from '../lib/pagedList';
  import ErrorMessage from './ErrorMessage.svelte';
  import Record from './WorkRecordItem.svelte';

  let {
    projectId,
    entry,
    changes = 0,
  }: {
    projectId: string;
    entry: EntryView;
    /** Counts the user's changes to the entry: after one, the first page is read again. */
    changes?: number;
  } = $props();
  const records = $state(emptyList<WorkRecord, number>());
  // The entry view is keyed by the entry and revision it shows, so its ID never changes here.
  const id = untrack(() => entry.entry_id);
  const list = new PagedList(records, async (before) => {
    const found = await api.ledgerWork(projectId, id, before, WORK_PAGE);
    return { items: found.work.records, next: found.work.next_before };
  });
  void list.reload();
  let seen = untrack(() => changes);
  $effect(() => {
    if (changes === seen) return;
    seen = changes;
    void list.reload();
  });
</script>

<h2>Work records</h2>
<p class="muted">What was tried on it, newest first, and how each attempt ended.</p>
{#if records.items.length}
  <ul class="work-records">
    {#each records.items as record (record.record_id)}
      <Record {projectId} {record} read={records.items} />
    {/each}
  </ul>
{:else if !records.loading && !records.error}
  <p class="muted">No work is recorded on it yet.</p>
{/if}
{#if records.loading}<p class="muted">Loading…</p>{/if}
{#if records.error}<ErrorMessage error={records.error} />{/if}
{#if records.next !== null && !records.loading}
  <button type="button" onclick={() => list.more()}>Older work records</button>
{/if}
