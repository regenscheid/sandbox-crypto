<!-- SPDX-License-Identifier: MIT -->
<!-- How a run's review ended (CR-48), in plain words, under its one-line summary. -->
<script lang="ts">
  import type { FinalReviewSummary } from '../lib/api';
  import { finalReviewEnd } from '../lib/reviews';

  let {
    summary,
  }: { summary: { no_final_review?: string; final_review?: FinalReviewSummary } | undefined } =
    $props();

  const lines = $derived(finalReviewEnd(summary));
</script>

{#if lines.length}
  <ul class="review-end" aria-label="How the run's review ended">
    {#each lines as line, index (index)}
      <li>{line}</li>
    {/each}
  </ul>
{/if}
