<!-- SPDX-License-Identifier: MIT -->
<!-- The results of a category under one set of conditions (docs/specs/ledger.md, LG-21,
     LG-23): the best known, those not compared, the dominated and the candidates, newest
     first, each kind with how many the answer leaves out. Only props, so a test renders it
     on the server. -->
<script lang="ts">
  import type { CategoryMeasure, FrontierGroup, FrontierResult } from '../lib/api-types';
  import { conditionsText, moreText } from '../lib/frontier';
  import Result from './FrontierResult.svelte';

  let {
    projectId,
    group,
    measures,
  }: { projectId: string; group: FrontierGroup; measures: readonly CategoryMeasure[] } = $props();
  // Results come in the order they were made; candidates read newest first.
  const candidates = $derived([...group.candidates].reverse());
</script>

{#snippet results(kind: string, items: readonly FrontierResult[])}
  {#each items as result (result.entry_id)}
    <Result {projectId} {result} {measures} />
  {/each}
  {#if moreText(group, kind)}<p class="muted">{moreText(group, kind)}</p>{/if}
{/snippet}

<section class="frontier-group">
  <h2>{conditionsText(group.conditions)}</h2>
  <h3>Best known</h3>
  {#if group.best.length || group.more.best}
    {@render results('best', group.best)}
  {:else}
    <p class="muted">None.</p>
  {/if}
  {#if group.not_compared.length || group.more.not_compared}
    <h3>Not compared</h3>
    <p class="muted">Eligible, with a measure missing or not a number.</p>
    {@render results('not_compared', group.not_compared)}
  {/if}
  {#if group.dominated.length || group.more.dominated}
    <h3>Dominated</h3>
    {@render results('dominated', group.dominated)}
  {/if}
  {#if group.candidates.length || group.more.candidates}
    <h3>Candidates</h3>
    <p class="muted">
      Not eligible: proposed results of this project, or disputed ones that never were eligible, and
      published results you have not confirmed. Newest first.
    </p>
    {@render results('candidates', candidates)}
  {/if}
</section>
