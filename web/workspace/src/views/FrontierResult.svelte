<!-- SPDX-License-Identifier: MIT -->
<!-- One result as the frontier shows it (docs/specs/ledger.md, LG-20, LG-23): its handle,
     leading to the entry at the revision shown, its title, status and marks, who wrote it,
     with the user's confirmation of an unverified one, its measures and indicators, what
     dominates it (by the rule, or by a link the user can drop) or what it would dominate,
     and its statement on request, rendered only once opened. All of it text, the
     statement through the Markdown component. -->
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
  import { ruleDominators } from '../lib/frontierLinks';
  import { ENTRY_NAME, ledgerEntryHref } from '../lib/router';
  import FrontierConfirm from './FrontierConfirm.svelte';
  import FrontierLink from './FrontierLink.svelte';
  import HiddenBanner from './HiddenBanner.svelte';

  let {
    projectId,
    result,
    measures,
    done,
  }: {
    projectId: string;
    result: FrontierResult;
    measures: readonly CategoryMeasure[];
    /** After the user's drop of a link or check: what to say, and the frontier read again. */
    done: (text: string) => Promise<void>;
  } = $props();
  let open = $state(false);
  let confirming = $state(false);
  const rule = $derived(ruleDominators(result));
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
    {#if confirming}
      <FrontierConfirm
        {projectId}
        {result}
        done={async (text) => {
          confirming = false;
          await done(text);
        }}
        oncancel={() => (confirming = false)}
      />
    {:else}
      <p><button type="button" onclick={() => (confirming = true)}>Confirm…</button></p>
    {/if}
  {:else}
    <p class="muted">Written by {writerText(result.author)}</p>
  {/if}
  <ul class="measures">
    {#each measuresText(result, measures) as line, index (index)}<li>{line}</li>{/each}
  </ul>
  <p class="muted">{indicatorsText(result).join('. ')}.</p>
  {#if result.sources.length}<p>Cites {@render entries(result.sources)}</p>{/if}
  {#if rule.length}
    <p>
      Dominated by the rule, at least as good on every measure and better on one: {@render entries(
        rule,
      )}
    </p>
  {/if}
  {#if result.linked_by.length}
    <ul class="links">
      {#each result.linked_by as link (link.entry_id)}
        <FrontierLink {projectId} {link} {done} />
      {/each}
    </ul>
  {/if}
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
