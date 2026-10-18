<!-- SPDX-License-Identifier: MIT -->
<!-- A ledger browser with list/detail as the default and the original board as an option.
     Entry routes keep the browser and project/session navigation visible. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { ENTRY_TYPES, STATUSES, statusText, TYPE_NAMES, type EntryType } from '../lib/ledger';
  import { lastSession } from '../lib/lastSession';
  import { frontierHref, issuesHref, ledgerHref, projectHref, sessionHref } from '../lib/router';
  import LedgerColumn from './LedgerColumn.svelte';
  import LedgerEntryView from './LedgerEntryView.svelte';
  import LedgerList from './LedgerList.svelte';
  import PaneResizer from './PaneResizer.svelte';

  let {
    projectId,
    entryName = null,
    revision = null,
  }: { projectId: string; entryName?: string | null; revision?: number | null } = $props();
  function typeForEntry(name: string | null): EntryType | null {
    const prefix = name?.split('-')[0];
    return (
      (
        {
          I: 'issue',
          C: 'claim',
          R: 'result',
          A: 'assumption',
          D: 'decision',
          T: 'task',
          REF: 'reference',
        } as Record<string, EntryType>
      )[prefix ?? ''] ?? null
    );
  }
  let type = $state<EntryType>(untrack(() => typeForEntry(entryName) ?? 'issue'));
  let status = $state('all');
  let mode = $state<'list' | 'board'>('list');
  let hidden = $state<Record<string, boolean>>({});
  let filter = $state('');
  let detailWidth = $state(560);
  let viewportWidth = $state(window.innerWidth);
  const maxDetailWidth = $derived(Math.max(320, Math.floor(viewportWidth / 2)));
  const recentSession = $derived(lastSession(projectId));
  const columns = $derived(STATUSES[type].filter((item) => !hidden[`${type}/${item}`]));

  function chooseType(next: EntryType) {
    type = next;
    status = 'all';
  }
  $effect(() => {
    const namedType = typeForEntry(entryName);
    untrack(() => {
      if (namedType && namedType !== type) chooseType(namedType);
    });
  });
</script>

<svelte:window
  onresize={() => {
    viewportWidth = window.innerWidth;
    detailWidth = Math.min(detailWidth, maxDetailWidth);
  }}
/>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
  {#if entryName}<a href={ledgerHref(projectId)}>Ledger</a> /{:else}Ledger /{/if}
</nav>
<div class="ledger-heading">
  <div>
    <h1>Research ledger</h1>
    <p class="muted">Issues, claims, results and decisions recorded by this project.</p>
  </div>
  <div class="ledger-mode" role="group" aria-label="Ledger view">
    <button type="button" aria-pressed={mode === 'list'} onclick={() => (mode = 'list')}
      >List</button
    >
    <button type="button" aria-pressed={mode === 'board'} onclick={() => (mode = 'board')}
      >Board</button
    >
  </div>
</div>

<div class="ledger-links">
  {#if recentSession}<a href={sessionHref(projectId, recentSession)}>Return to {recentSession}</a
    >{/if}
  <a href={issuesHref(projectId)}>Issue list</a>
  <a href={frontierHref(projectId)}>Research frontier</a>
</div>

<div class="ledger-browser-controls">
  <label>
    Entry type
    <select value={type} onchange={(event) => chooseType(event.currentTarget.value as EntryType)}>
      {#each ENTRY_TYPES as option (option)}<option value={option}>{TYPE_NAMES[option]}</option
        >{/each}
    </select>
  </label>
  {#if mode === 'list'}
    <label>
      Status
      <select bind:value={status}>
        <option value="all">All statuses</option>
        {#each STATUSES[type] as option (option)}
          <option value={option}>{statusText(option)}</option>
        {/each}
      </select>
    </label>
  {/if}
  <label class="ledger-search">
    Search loaded entries
    <input type="search" bind:value={filter} placeholder="Handle or title" />
  </label>
</div>

{#if mode === 'board'}
  <fieldset class="ledger-board-statuses">
    <legend>Statuses shown</legend>
    {#each STATUSES[type] as item (item)}
      <label
        ><input
          type="checkbox"
          checked={!hidden[`${type}/${item}`]}
          onchange={(event) => (hidden[`${type}/${item}`] = !event.currentTarget.checked)}
        />
        {statusText(item)}</label
      >
    {/each}
  </fieldset>
  <div class="board">
    {#each columns as item (`${type}/${item}`)}
      <LedgerColumn {projectId} {type} status={item} {filter} onselect={() => (mode = 'list')} />
    {:else}
      <p>Every status is hidden.</p>
    {/each}
  </div>
{:else}
  <div
    class="ledger-split"
    style:--ledger-detail-width={`${Math.min(detailWidth, maxDetailWidth)}px`}
  >
    <section class="ledger-list-pane" aria-label="Ledger entries">
      {#key `${projectId}/${type}/${status}`}
        <LedgerList {projectId} {type} {status} search={filter} selected={entryName} />
      {/key}
    </section>
    <PaneResizer bind:width={detailWidth} maxWidth={maxDetailWidth} label="Resize ledger detail" />
    <section class="ledger-detail-pane" aria-label="Ledger entry detail">
      {#if entryName}
        {#key `${entryName}/${revision}`}
          <LedgerEntryView {projectId} {entryName} {revision} compact />
        {/key}
      {:else}
        <div class="ledger-empty-detail">
          <h2>Select an entry</h2>
          <p class="muted">Its statement, evidence, flags and history will appear here.</p>
        </div>
      {/if}
    </section>
  </div>
{/if}
