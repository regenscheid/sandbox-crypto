<!-- SPDX-License-Identifier: MIT -->
<!-- The chosen review: what it concluded and covered, its pinned evidence and audit records.
     Context a ledger entry's evidence pins (CR-14 as amended for lab 40's B3) is marked as
     such, not reviewed material, and leads to its target where the workspace has a page; a
     delegation's outcome read whole shows as the run tree shows a delegation. -->
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
  import { contextLinks, contextTarget, contextText, outcomeOf } from '../lib/reviewContext';
  import DelegationCard from './DelegationCard.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  import PinnedEvidencePreview from './PinnedEvidencePreview.svelte';
  let {
    panel,
    browser,
    review,
    busy,
    projectId = '',
    sessionId = '',
  }: {
    panel: ReviewPanel;
    browser: ReviewBrowser;
    review: Review;
    busy: boolean;
    /** The project and session, to lead to a context item's target. */
    projectId?: string;
    sessionId?: string;
  } = $props();
  /** The item whose text is shown, once its first slice is: the read replaces the text then. */
  const shownItem = $derived.by(() => {
    void panel.text.items;
    const id = browser.text.shown();
    return panel.evidence.items.find((item) => item.evidence === id) ?? null;
  });
  /** A delegation's outcome read whole, shown as the run tree shows a delegation. */
  const outcome = $derived(
    shownItem?.kind === 'delegation' && panel.text.next === null
      ? outcomeOf(browser.text.text())
      : null,
  );
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
        {#if contextText(item)}
          <span class="context">{contextText(item)}</span>
          {contextTarget(item) ||
            visible(evidenceText(item))}{#each contextLinks(projectId, item) as link, index (index)}
            · <a href={link.href}>{link.text}</a>{/each}
        {:else}
          {visible(evidenceText(item))}
        {/if}
        <details>
          <summary>Locator as recorded</summary>
          <pre>{visible(JSON.stringify(item.locator, null, 2))}</pre>
        </details>
      </li>{/each}
  </ul>
  {#if panel.evidence.next !== null}<button disabled={busy} onclick={() => browser.evidence.more()}
      >Load more snapshot evidence</button
    >{/if}
  {#if panel.evidence.error}<ErrorMessage error={panel.evidence.error} />{/if}
  <!-- The evidence text read from this review's list, here beside it. -->
  {#if panel.text.items.length}{#if shownItem && contextText(shownItem)}<p class="context">
        {contextText(shownItem)}
      </p>{/if}{#if outcome}<DelegationCard
        {projectId}
        {sessionId}
        delegation={outcome}
      />{:else}<PinnedEvidencePreview
        text={browser.text.text()}
      />{/if}{#if panel.text.next !== null}<button onclick={() => browser.text.list.more()}
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
  .context {
    font-style: italic;
  }
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
