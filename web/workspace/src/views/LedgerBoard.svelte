<!-- SPDX-License-Identifier: MIT -->
<!-- The project's ledger as a board (docs/specs/ledger.md, LG-34): the entries of one type,
     a column for each status, filtered by status and by words in the handle or title.
     Read-only: agents change the ledger through their tools. -->
<script lang="ts">
  import { ENTRY_TYPES, STATUSES, statusText, TYPE_NAMES, type EntryType } from '../lib/ledger';
  import { issuesHref, projectHref } from '../lib/router';
  import LedgerColumn from './LedgerColumn.svelte';

  let { projectId }: { projectId: string } = $props();
  let type = $state<EntryType>('issue');
  /** The statuses hidden on the board, per type: view settings only. */
  let hidden = $state<Record<string, boolean>>({});
  let filter = $state('');
  const columns = $derived(STATUSES[type].filter((status) => !hidden[`${type}/${status}`]));
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
</nav>
<h1>Ledger</h1>
<p class="muted">
  The project's research state: its issues, claims and results, assumptions, decisions, tasks and
  references. Agents change it through their tools; here it is read-only.
</p>
<p><a href={issuesHref(projectId)}>The issue list</a>: issues and what they concern.</p>

<div class="board-controls">
  <div role="group" aria-label="Entry type">
    {#each ENTRY_TYPES as option (option)}
      <button type="button" aria-pressed={type === option} onclick={() => (type = option)}
        >{TYPE_NAMES[option]}</button
      >
    {/each}
  </div>
  <fieldset>
    <legend>Statuses shown</legend>
    {#each STATUSES[type] as status (status)}
      <label
        ><input
          type="checkbox"
          checked={!hidden[`${type}/${status}`]}
          onchange={(event) => (hidden[`${type}/${status}`] = !event.currentTarget.checked)}
        />
        {statusText(status)}</label
      >
    {/each}
  </fieldset>
  <label>Filter <input type="search" bind:value={filter} placeholder="handle or title" /></label>
</div>

<div class="board">
  {#each columns as status (`${type}/${status}`)}
    <LedgerColumn {projectId} {type} {status} {filter} />
  {:else}
    <p>Every status is hidden.</p>
  {/each}
</div>
