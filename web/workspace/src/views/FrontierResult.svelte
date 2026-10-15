<!-- SPDX-License-Identifier: MIT -->
<!-- One result as the frontier shows it (docs/specs/ledger.md, LG-20, LG-23): its handle,
     leading to the entry at the revision shown, its title, status and marks, who wrote it,
     its measures and indicators, the results it is dominated by or would dominate, and its
     statement on request, rendered only once opened. All of it text, the statement through
     the Markdown component. Only props, so a test renders it on the server. -->
<script lang="ts">
  import type { CategoryMeasure, FrontierResult } from '../lib/api-types';
  import {
    disputedText,
    indicatorsText,
    measuresText,
    unverifiedText,
    writerText,
  } from '../lib/frontier';
  import { statusText } from '../lib/ledger';
  import Markdown from '../lib/Markdown.svelte';
  import { ENTRY_NAME, ledgerEntryHref } from '../lib/router';
  import HiddenBanner from './HiddenBanner.svelte';

  let {
    projectId,
    result,
    measures,
  }: { projectId: string; result: FrontierResult; measures: readonly CategoryMeasure[] } = $props();
  let open = $state(false);
</script>

{#snippet entries(handles: readonly string[])}
  {#each handles as handle, index (index)}{index > 0 ? ', ' : ''}{#if ENTRY_NAME.test(handle)}<a
        href={ledgerEntryHref(projectId, handle)}>{handle}</a
      >{:else}{handle}{/if}{/each}
{/snippet}

<article class="card frontier-result">
  <p>
    {#if ENTRY_NAME.test(result.handle)}<a
        href={ledgerEntryHref(projectId, result.handle, result.revision)}>{result.handle}</a
      >{:else}{result.handle}{/if}
    r{result.revision}: {result.title}
    <span class="muted"
      >{statusText(result.status)}, {result.origin === 'published'
        ? 'published'
        : 'from this project'}</span
    >
  </p>
  {#if result.disputed}<p><strong class="flagged">{disputedText(result)}</strong></p>{/if}
  {#if result.unverified}
    <p><strong class="flagged">{unverifiedText(result)}</strong></p>
  {:else}
    <p class="muted">Written by {writerText(result.author)}</p>
  {/if}
  <ul class="measures">
    {#each measuresText(result, measures) as line, index (index)}<li>{line}</li>{/each}
  </ul>
  <p class="muted">{indicatorsText(result).join('. ')}.</p>
  {#if result.sources.length}<p>Cites {@render entries(result.sources)}</p>{/if}
  {#if result.dominated_by.length}<p>Dominated by {@render entries(result.dominated_by)}</p>{/if}
  {#if result.would_dominate.length}
    <p>Would dominate, once eligible: {@render entries(result.would_dominate)}</p>
  {/if}
  <details ontoggle={(event) => (open = event.currentTarget.open)}>
    <summary>Statement</summary>
    {#if open}
      <Markdown source={result.statement} />
      <HiddenBanner text={result.statement} links />
    {/if}
  </details>
</article>
