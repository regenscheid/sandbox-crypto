<!-- SPDX-License-Identifier: MIT -->
<!-- A filterable list of ledger entries. The API pages by status, so search is explicitly
     scoped to loaded rows; each unfinished status can be paged without losing other rows. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { EntrySummary } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { matches, STATUSES, statusText, type EntryType } from '../lib/ledger';
  import { openFlagsText } from '../lib/ledgerFlags';
  import { ledgerEntryHref } from '../lib/router';
  import { entryTags } from '../lib/researchState';
  import ErrorMessage from './ErrorMessage.svelte';
  import StandingBadge from './StandingBadge.svelte';

  let {
    projectId,
    type,
    status,
    search,
    selected,
  }: {
    projectId: string;
    type: EntryType;
    status: string;
    search: string;
    selected: string | null;
  } = $props();

  type Page = { items: EntrySummary[]; next: string | null; loading: boolean; error: unknown };
  let pages = $state<Record<string, Page>>({});
  let generation = 0;
  const statuses = $derived(status === 'all' ? STATUSES[type] : [status]);
  const loaded = $derived(statuses.flatMap((item) => pages[item]?.items ?? []));
  const shown = $derived(
    loaded
      .filter((item) => matches(item, search))
      .sort((a, b) => b.updated.localeCompare(a.updated) || a.handle.localeCompare(b.handle)),
  );
  const unfinished = $derived(statuses.filter((item) => pages[item]?.next && !pages[item]?.error));

  async function load(pageStatus: string, after: string | null, epoch: number) {
    const current = pages[pageStatus];
    if (!current || current.loading) return;
    pages[pageStatus] = { ...current, loading: true, error: null };
    try {
      const result = await api.ledger(projectId, type, pageStatus, after);
      if (epoch !== generation) return;
      pages[pageStatus] = {
        items: after === null ? result.page.entries : [...current.items, ...result.page.entries],
        next: result.page.next_after,
        loading: false,
        error: null,
      };
    } catch (error) {
      if (epoch !== generation) return;
      pages[pageStatus] = { ...current, loading: false, error };
    }
  }

  $effect(() => {
    // A new type or status selection starts a new, independent set of pages.
    const selectedType = type;
    const selectedStatus = status;
    const selectedProject = projectId;
    untrack(() => {
      const epoch = ++generation;
      const targets = selectedStatus === 'all' ? STATUSES[selectedType] : [selectedStatus];
      pages = Object.fromEntries(
        targets.map((item) => [item, { items: [], next: null, loading: false, error: null }]),
      );
      void selectedProject;
      for (const item of targets) void load(item, null, epoch);
    });
  });
</script>

<div class="ledger-list-count muted">
  {shown.length} matching {loaded.length} loaded entries
  {#if unfinished.length}
    · more pages available{/if}
</div>
<div class="ledger-list-items">
  {#each shown as entry (entry.entry_id)}
    <a
      class="ledger-list-row"
      class:selected={selected === entry.handle || selected === entry.entry_id}
      href={ledgerEntryHref(projectId, entry.handle)}
      aria-current={selected === entry.handle || selected === entry.entry_id ? 'true' : undefined}
    >
      <span class="ledger-list-row-top">
        <strong>{entry.handle}</strong>
        <span class="ledger-status">{statusText(entry.status)}</span>
      </span>
      <span class="ledger-list-title">{entry.title}</span>
      <span class="ledger-list-row-bottom muted">
        <StandingBadge {projectId} standing={entry.standing} tags={entryTags(entry)} />
        {#if entry.open_flags > 0}<span class="flagged">{openFlagsText(entry.open_flags)}</span
          >{/if}
        {#if entry.asserted_only}<span>asserted only</span>{/if}
        <span>r{entry.revision} · {formatTime(entry.updated)}</span>
      </span>
    </a>
  {:else}
    {#if !Object.values(pages).some((page) => page.loading)}
      <p class="muted">No entries match the current filters in the loaded pages.</p>
    {/if}
  {/each}
</div>
{#if Object.values(pages).some((page) => page.loading)}<p class="muted">Loading…</p>{/if}
{#each statuses as item (item)}
  {#if pages[item]?.error}
    <strong>{statusText(item)}</strong>
    <ErrorMessage error={pages[item].error} />
    <button type="button" onclick={() => load(item, pages[item]?.next ?? null, generation)}
      >Retry {statusText(item)}</button
    >
  {/if}
{/each}
{#if unfinished.length}
  <button
    type="button"
    disabled={unfinished.some((item) => pages[item]?.loading)}
    onclick={() => {
      const epoch = generation;
      for (const item of unfinished) {
        const next = pages[item]?.next;
        if (next) void load(item, next, epoch);
      }
    }}>Load more entries</button
  >
{/if}
