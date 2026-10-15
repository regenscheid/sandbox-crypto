<!-- SPDX-License-Identifier: MIT -->
<!--
  A session's consistency reviews, math checks and findings (docs/CONSISTENCY-REVIEW.md),
  in four parts that share one reading (reviewBrowser.ts). Each part shows its own
  failures beside its controls.
-->
<script lang="ts">
  import { untrack } from 'svelte';
  import { emptyReviewPanel, panelBusy, ReviewBrowser } from '../lib/reviewBrowser';
  import ErrorMessage from './ErrorMessage.svelte';
  import FindingList from './FindingList.svelte';
  import MathChecks from './MathChecks.svelte';
  import ReviewDetail from './ReviewDetail.svelte';
  import ReviewList from './ReviewList.svelte';
  let {
    projectId,
    sessionId,
    reviewId = null,
  }: { projectId: string; sessionId: string; reviewId?: string | null } = $props();
  const panel = $state(emptyReviewPanel());
  const browser = new ReviewBrowser(panel, () => ({ project: projectId, session: sessionId }));
  const busy = $derived(panelBusy(panel));
  // The Refresh button clears every failure shown; the effect below only reads the lists.
  function refreshAll() {
    browser.clearErrors();
    void browser.refresh();
  }
  $effect(() => {
    void browser.refresh();
  });
  let section = $state<HTMLElement | null>(null);
  // A link names one review: choose it once, and bring the panel into view. The session
  // view is keyed by the review the link names, so it never changes here.
  const linked = untrack(() => reviewId);
  if (linked !== null) {
    void browser.open(linked).then(() => section?.scrollIntoView?.());
  }
</script>

<section aria-label="Consistency reviews" bind:this={section}>
  <h2>Consistency reviews</h2>
  <p class="muted">
    A pass covers the recorded checks and is not a proof. Missing evidence, capped reviews and
    unconfirmed fixes remain explicit.
  </p>
  <button disabled={busy} onclick={refreshAll}>Refresh reviews</button><button
    disabled={busy}
    onclick={() => browser.request()}>Request review at next live safe point</button
  >
  {#if panel.notice}<p role="status">{panel.notice}</p>{/if}
  {#if panel.requestError}<ErrorMessage error={panel.requestError} />{/if}
  <ReviewList {panel} {browser} {busy} />
  {#if panel.reviews.error}<ErrorMessage error={panel.reviews.error} />{/if}
  {#if panel.selected}<ReviewDetail
      {panel}
      {browser}
      review={panel.selected}
      {busy}
      {projectId}
      {sessionId}
    />{/if}
  <MathChecks {panel} {browser} />
  <FindingList {projectId} {sessionId} {panel} {browser} {busy} />
</section>

<style>
  section {
    border-top: 1px solid var(--border, #9996);
    margin-top: 1rem;
  }
  section :global(button) {
    margin: 0.25rem;
  }
  section :global(code) {
    overflow-wrap: anywhere;
  }
</style>
