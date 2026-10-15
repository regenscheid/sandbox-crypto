<!-- SPDX-License-Identifier: MIT -->
<!-- The chosen review: what it concluded and covered, its pinned evidence and audit records. -->
<script lang="ts">
  import type { ReviewBrowser, ReviewPanel } from '../lib/reviewBrowser';
  import { visible } from '../lib/text';
  import type { Review } from '../lib/reviews';
  import {
    coverageText,
    evidenceText,
    identityText,
    leftUnreviewed,
    outcomeText,
    recordText,
    reviewLine,
    usageText,
  } from '../lib/reviewText';
  import ErrorMessage from './ErrorMessage.svelte';
  import PinnedEvidencePreview from './PinnedEvidencePreview.svelte';
  let {
    panel,
    browser,
    review,
    busy,
  }: { panel: ReviewPanel; browser: ReviewBrowser; review: Review; busy: boolean } = $props();
</script>

<section class="detail" aria-label="Review detail">
  <h3>{reviewLine(review)}</h3>
  <p>
    <strong>Outcome:</strong>
    {outcomeText(review.state, leftUnreviewed(panel.unreviewed, review.review_id))}.
  </p>
  {#if review.summary}<p class="prose">{visible(review.summary)}</p>{/if}
  <p>{coverageText(review)}</p>
  <p class="muted">{usageText(review)}</p>
  <details>
    <summary>{identityText(review)}</summary>
    <pre>{JSON.stringify(
        { classifier: review.classifier, config_revision: review.config_revision },
        null,
        2,
      )}</pre>
  </details>
  <h4>Pinned evidence</h4>
  {#if !panel.evidence.items.length && !panel.evidence.loading && panel.evidence.error === null}<p
      class="muted"
    >
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
  <!-- The evidence text read from this review's list, here beside it. -->
  {#if panel.text.items.length}<PinnedEvidencePreview
      text={browser.text.text()}
    />{#if panel.text.next !== null}<button onclick={() => browser.text.list.more()}
        >Read next evidence slice</button
      >{/if}{/if}
  {#if panel.text.error}<ErrorMessage error={panel.text.error} />{/if}
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
