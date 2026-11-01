<!-- SPDX-License-Identifier: MIT -->
<!-- The session's reviews: the independent checks its agents asked for (delegations of kind
     check, PD-42), newest first, each with the entry it checks and its verdict; and the
     consistency reviews with their findings (ReviewsPanel.svelte). The verdicts and
     summaries are agents' text, shown with what a person may not see as its code point. -->
<script lang="ts">
  import type { Delegation, RunTree } from '../lib/api-types';
  import { clock, firstLine } from '../lib/conversation';
  import { ledgerEntryHref } from '../lib/router';
  import { briefView, resultView } from '../lib/runTree';
  const reviewComponent = import('./ReviewsPanel.svelte');

  let {
    projectId,
    sessionId,
    reviewId = null,
    tree,
  }: {
    projectId: string;
    sessionId: string;
    reviewId?: string | null;
    tree: RunTree | null;
  } = $props();
  const checks = $derived(
    [...(tree?.delegations ?? [])].filter((item) => item.kind === 'check').reverse(),
  );

  function verdict(check: Delegation): { text: string; tone: string } {
    if (check.ended === null) return { text: 'Checking', tone: 'info' };
    const said = resultView(check)?.verdict ?? '';
    if (!said) return { text: check.ended === 'returned' ? 'No verdict' : 'Ended', tone: '' };
    const tone = /refut|fail|wrong|false/i.test(said)
      ? 'failed'
      : /inconclusive|partial|unclear|undecided/i.test(said)
        ? 'warn'
        : /hold|confirm|reproduc|pass|true|support/i.test(said)
          ? 'ok'
          : '';
    return { text: said.charAt(0).toUpperCase() + said.slice(1), tone };
  }
</script>

<section aria-label="Session reviews">
  <h2>Independent checks</h2>
  <p class="muted lead">
    Before a result counts, a separate agent re-derives it from its evidence.
  </p>
  {#each checks as check (check.delegation_id)}
    {@const brief = briefView(check)}
    {@const shown = verdict(check)}
    {@const result = resultView(check)}
    <article class="agent-card">
      <div class="agent-heading">
        {#if brief.entry}<a
            href={ledgerEntryHref(projectId, brief.entry.handle, brief.entry.revision ?? undefined)}
            ><strong>{brief.entry.handle}</strong></a
          >{:else}<strong>Check</strong>{/if}
        <span class="pill {shown.tone}">{shown.text}</span>
        <span class="muted"
          >{check.ended_at ? clock(check.ended_at) : `since ${clock(check.started_at)}`}</span
        >
      </div>
      {#if brief.task && !brief.entry}<p class="agent-task">{firstLine(brief.task, 240)}</p>{/if}
      {#if result?.summary}<p class="agent-detail">{firstLine(result.summary, 320)}</p>{/if}
    </article>
  {:else}
    <p class="empty-note">No independent checks yet.</p>
  {/each}
  {#await reviewComponent then component}{@const Reviews = component.default}<Reviews
      {projectId}
      {sessionId}
      {reviewId}
    />{/await}
</section>

<style>
  .lead {
    margin: 0 0 12px;
  }
</style>
