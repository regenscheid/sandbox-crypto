<!-- SPDX-License-Identifier: MIT -->
<!-- The chosen review: what it concluded and covered, its pinned evidence and audit records. -->
<script lang="ts">
  import type { ReviewBrowser, ReviewPanel } from '../lib/reviewBrowser';
  import type { Review } from '../lib/reviews';
  import {
    coverageText,
    evidenceText,
    outcomeText,
    recordText,
    reviewLine,
    usageText,
  } from '../lib/reviewText';
  import ErrorMessage from './ErrorMessage.svelte';
  let {
    panel,
    browser,
    review,
    busy,
  }: { panel: ReviewPanel; browser: ReviewBrowser; review: Review; busy: boolean } = $props();
</script>

<section class="detail" aria-label="Review detail">
  <h3>{reviewLine(review)}</h3>
  <p><strong>Outcome:</strong> {outcomeText(review.state)}.</p>
  {#if review.summary}<p class="prose">{review.summary}</p>{/if}
  <p>{coverageText(review)}</p>
  <p class="muted">{usageText(review)}</p>
  <details>
    <summary>Classifier and configuration identity</summary>
    <pre>{JSON.stringify(
        { classifier: review.classifier, config_revision: review.config_revision },
        null,
        2,
      )}</pre>
  </details>
  <h4>Pinned evidence</h4>
  {#if !panel.evidence.items.length && !panel.evidence.loading}<p class="muted">
      No evidence pinned.
    </p>{/if}
  <ul>
    {#each panel.evidence.items as item (item.evidence)}<li>
        <button onclick={() => browser.read(review.review_id, item.evidence)}>Read</button>
        {evidenceText(item)}
        <details>
          <summary>Locator as recorded</summary>
          <pre>{JSON.stringify(item.locator, null, 2)}</pre>
        </details>
      </li>{/each}
  </ul>
  {#if panel.evidence.next !== null}<button disabled={busy} onclick={() => browser.evidence.more()}
      >Load more snapshot evidence</button
    >{/if}
  {#if panel.evidence.error}<ErrorMessage error={panel.evidence.error} />{/if}
  <h4>Audit records</h4>
  <button disabled={busy || panel.records.next === null} onclick={() => browser.records.more()}
    >{panel.records.items.length ? 'Load more audit records' : 'Load review audit records'}</button
  >
  <ul>
    {#each panel.records.items as record, index (index)}<li>
        <details>
          <summary>{recordText(record)}</summary>
          <pre>{JSON.stringify(record, null, 2)}</pre>
        </details>
      </li>{/each}
  </ul>
  {#if panel.records.error}<ErrorMessage error={panel.records.error} />{/if}
</section>

<style>
  .detail {
    border-left: 3px solid var(--border, #9996);
    padding-left: 0.8rem;
  }
  pre,
  .prose {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  pre {
    max-height: 22rem;
    overflow: auto;
  }
</style>
