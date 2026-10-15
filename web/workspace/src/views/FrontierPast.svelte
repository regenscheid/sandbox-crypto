<!-- SPDX-License-Identifier: MIT -->
<!-- A category's best known results as they were at a time the user picks (docs/specs/
     ledger.md, LG-24): the time read in the browser's zone and sent with it, then each
     group of conditions with its best known then, each leading to its entry at the
     revision recorded. Only the latest pick is shown. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { FrontierPastAnswer } from '../lib/api-types';
  import { Detail, noDetail } from '../lib/detail';
  import { formatTime } from '../lib/format';
  import { conditionsText } from '../lib/frontier';
  import { pastTime } from '../lib/frontierHistory';
  import { ENTRY_NAME, ledgerEntryHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId, category }: { projectId: string; category: string } = $props();
  let local = $state('');
  let problem = $state<string | null>(null);
  const read = $state(noDetail<string, FrontierPastAnswer>());
  // The frontier view is keyed by its project and category: they never change here.
  const past = new Detail(read, (at: string) => api.frontierPast(projectId, category, at));

  function show(): void {
    const at = pastTime(local);
    problem = at === null ? 'Pick a date and a time.' : null;
    if (at !== null) void past.show(at);
  }
</script>

<h2>Best known at a time</h2>
<p>
  <label
    >The best known results at <input type="datetime-local" step="1" bind:value={local} /></label
  >
  <button type="button" disabled={read.loading} onclick={show}>Show</button>
</p>
{#if problem}<p class="problem" role="alert">{problem}</p>{/if}
{#if read.loading}<p class="muted">Loading…</p>{/if}
{#if read.error}<ErrorMessage error={read.error} />{/if}
{#if read.value}
  {@const found = read.value.past}
  <p>As the history left them at {formatTime(found.at)}:</p>
  <ul>
    {#each found.groups as group, index (index)}
      <li>
        {conditionsText(group.conditions)}: {#each group.best as member, inner (inner)}{inner > 0
            ? ', '
            : ''}{#if ENTRY_NAME.test(member.handle)}<a
              href={ledgerEntryHref(projectId, member.handle, member.revision)}
              >{member.handle} r{member.revision}</a
            >{:else}{member.handle} r{member.revision}{/if}{:else}none{/each}{group.more
          ? ` and ${group.more} more`
          : ''}
      </li>
    {:else}
      <li class="muted">No best known result was recorded by then.</li>
    {/each}
  </ul>
{/if}
