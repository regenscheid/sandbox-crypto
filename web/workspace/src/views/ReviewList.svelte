<!-- SPDX-License-Identifier: MIT -->
<!-- The session's consistency reviews, newest first, and the windows none could cover. -->
<script lang="ts">
  import type { ReviewBrowser, ReviewPanel } from '../lib/reviewBrowser';
  import { visible } from '../lib/text';
  import {
    leftUnreviewed,
    outcomeText,
    reviewLine,
    unreviewedText,
    waitingText,
  } from '../lib/reviewText';
  let { panel, browser, busy }: { panel: ReviewPanel; browser: ReviewBrowser; busy: boolean } =
    $props();
</script>

{#if panel.waiting}<p role="status" class="waiting">{waitingText(panel.waiting)}</p>{/if}
{#if panel.unreviewed.length}
  <div role="status" class="unreviewed">
    <h3>Left unreviewed</h3>
    <ul>
      {#each panel.unreviewed as window, index (index)}<li>{unreviewedText(window)}</li>{/each}
    </ul>
    <details>
      <summary>Unreviewed windows as recorded</summary>
      <pre>{JSON.stringify(panel.unreviewed, null, 2)}</pre>
    </details>
  </div>
{/if}
{#if !panel.reviews.items.length && !panel.reviews.loading && panel.reviews.error === null}<p>
    No consistency reviews recorded.
  </p>{/if}
<ul class="reviews">
  {#each panel.reviews.items as review (review.review_id)}<li>
      <button
        aria-pressed={panel.selected?.review_id === review.review_id}
        onclick={() => browser.select(review)}>{reviewLine(review)}</button
      >
      <span class="muted"
        >{outcomeText(review.state, leftUnreviewed(panel.unreviewed, review.review_id))}.</span
      >
      {#if review.summary}<p class="summary">{visible(review.summary)}</p>{/if}
    </li>{/each}
</ul>
{#if panel.reviews.next}<button disabled={busy} onclick={() => browser.reviews.more()}
    >Load more reviews</button
  >{/if}

<style>
  .reviews {
    padding-left: 1rem;
  }
  .summary {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    margin: 0.2rem 0 0.6rem;
  }
  .unreviewed {
    border-left: 3px solid #b3261e;
    padding-left: 0.8rem;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 16rem;
    overflow: auto;
  }
</style>
