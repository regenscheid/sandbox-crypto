<!-- SPDX-License-Identifier: MIT -->
<!-- A category's changes to its best known results, newest first, read on request 20 at
     a time (docs/specs/ledger.md, LG-24); after the user's change on the page, the first
     page is read again. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { FrontierChange } from '../lib/api-types';
  import { emptyList, PagedList } from '../lib/pagedList';
  import Change from './FrontierChange.svelte';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    category,
    changes = 0,
  }: {
    projectId: string;
    category: string;
    /** Counts the user's changes on the page: after one, the first page is read again. */
    changes?: number;
  } = $props();
  const history = $state(emptyList<FrontierChange, number>());
  let started = $state(false);
  // The frontier view is keyed by its project and category: they never change here.
  const list = new PagedList(history, async (before) => {
    const found = await api.frontierHistory(projectId, category, before);
    return { items: found.history.changes, next: found.history.next_before };
  });

  function show(): void {
    started = true;
    void list.reload();
  }

  let seen = untrack(() => changes);
  $effect(() => {
    if (changes === seen) return;
    seen = changes;
    if (untrack(() => started)) void list.reload();
  });
</script>

<h2>History</h2>
{#if !started}
  <p>
    <button type="button" onclick={show}>Show the history</button>
    <span class="muted">Each change to the best known results, newest first.</span>
  </p>
{:else}
  <ul class="frontier-history">
    {#each history.items as change (change.seq)}
      <Change {projectId} {change} />
    {/each}
  </ul>
  {#if !history.loading && !history.error && history.items.length === 0}
    <p class="muted">No change is recorded yet.</p>
  {/if}
  {#if history.loading}<p class="muted">Loading…</p>{/if}
  {#if history.error}<ErrorMessage error={history.error} />{/if}
  {#if history.next !== null}
    <button type="button" disabled={history.loading} onclick={() => list.more()}
      >Older changes</button
    >
  {/if}
{/if}
