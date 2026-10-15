<!-- SPDX-License-Identifier: MIT -->
<!-- The links other entries' current revisions hold to an entry at its current revision:
     the first page comes with the entry, the rest a page at a time, with their count, each
     link shown once. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { EntryView } from '../lib/api-types';
  import { incomingLinks, incomingState, linksByKind, uniqueLinks } from '../lib/ledger';
  import ErrorMessage from './ErrorMessage.svelte';
  import LedgerLinks from './LedgerLinks.svelte';

  let { projectId, entry }: { projectId: string; entry: EntryView } = $props();
  // The entry view is keyed by the entry and revision it shows: this one never changes,
  // and its first page of links starts the list.
  const initial = untrack(() => entry);
  const links = $state(incomingState(initial));
  const list = incomingLinks(links, async (after) => {
    const found = await api.ledgerEntry(projectId, initial.entry_id, null, after);
    return found.entry;
  });
  const shown = $derived(uniqueLinks(links.items));
</script>

{#if shown.length}
  <h3>Links to it <span class="muted">({links.total})</span></h3>
  <LedgerLinks {projectId} groups={linksByKind(shown)} direction="in" />
  {#if links.next !== null}
    <button type="button" disabled={links.loading} onclick={() => list.more()}
      >More links to it</button
    >
  {/if}
{:else}
  <p class="muted">No entry links to it.</p>
{/if}
{#if links.error}<ErrorMessage error={links.error} />{/if}
