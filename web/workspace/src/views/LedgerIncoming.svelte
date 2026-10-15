<!-- SPDX-License-Identifier: MIT -->
<!-- The links other entries' current revisions hold to an entry at its current revision:
     the first page comes with the entry, the rest a page at a time, with their count. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { EntryLink, EntryView } from '../lib/api-types';
  import { incomingPage, linksByKind } from '../lib/ledger';
  import { PagedList, type ListState } from '../lib/pagedList';
  import ErrorMessage from './ErrorMessage.svelte';
  import LedgerLinks from './LedgerLinks.svelte';

  let { projectId, entry }: { projectId: string; entry: EntryView } = $props();
  // The entry view is keyed by the entry and revision it shows: this one never changes,
  // and its first page of links starts the list.
  const initial = untrack(() => entry);
  const first = incomingPage(initial, 0);
  const links = $state<ListState<EntryLink, number>>({
    items: first.items,
    next: first.next,
    loading: false,
    error: null,
  });
  let total = $state(initial.incoming_total);
  const list = new PagedList(links, async (after) => {
    const found = await api.ledgerEntry(projectId, initial.entry_id, null, after ?? 0);
    return {
      ...incomingPage(found.entry, after ?? 0),
      shown: () => (total = found.entry.incoming_total),
    };
  });
</script>

{#if links.items.length}
  <h3>Links to it <span class="muted">({total})</span></h3>
  <LedgerLinks {projectId} groups={linksByKind(links.items)} direction="in" />
  {#if links.next !== null}
    <button type="button" disabled={links.loading} onclick={() => list.more()}
      >More links to it</button
    >
  {/if}
{:else}
  <p class="muted">No entry links to it.</p>
{/if}
{#if links.error}<ErrorMessage error={links.error} />{/if}
