<!-- SPDX-License-Identifier: MIT -->
<!-- One issue in the list: its handle and title, opening to read what it concerns, what
     answers it and what it was split into, from the issue at its current revision. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { EntrySummary, EntryView } from '../lib/api-types';
  import { Detail, noDetail } from '../lib/detail';
  import { formatTime } from '../lib/format';
  import { linksByKind } from '../lib/ledger';
  import { ledgerEntryHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import LedgerLinks from './LedgerLinks.svelte';

  let { projectId, issue }: { projectId: string; issue: EntrySummary } = $props();
  const shown = $state(noDetail<string, EntryView>());
  const detail = new Detail(shown, async (entry) => {
    const found = await api.ledgerEntry(projectId, entry);
    return found.entry;
  });
</script>

<li>
  <details
    ontoggle={(event) => {
      if (event.currentTarget.open && shown.value === null && !shown.loading)
        void detail.show(issue.entry_id);
    }}
  >
    <summary
      ><a href={ledgerEntryHref(projectId, issue.handle)}>{issue.handle}</a>
      {issue.title}
      <span class="muted">r{issue.revision} · {formatTime(issue.updated)}</span></summary
    >
    {#if shown.loading}<p class="muted">Loading…</p>{/if}
    {#if shown.error}<ErrorMessage error={shown.error} />{/if}
    {#if shown.value}
      {@const groups = {
        out: linksByKind(shown.value.links),
        in: linksByKind(shown.value.incoming),
      }}
      {#if groups.out.length}<LedgerLinks {projectId} groups={groups.out} direction="out" />{/if}
      {#if groups.in.length}<LedgerLinks {projectId} groups={groups.in} direction="in" />{/if}
      {#if shown.value.incoming_total > shown.value.incoming.length}<p class="muted">
          And {shown.value.incoming_total - shown.value.incoming.length} more links to it:
          <a href={ledgerEntryHref(projectId, issue.handle)}>open {issue.handle}</a>.
        </p>{/if}
      {#if !groups.out.length && !groups.in.length}<p class="muted">
          It concerns nothing named, and nothing answers it yet.
        </p>{/if}
    {/if}
  </details>
</li>
