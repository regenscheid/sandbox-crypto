<!-- SPDX-License-Identifier: MIT -->
<!-- A quote's citation checks (docs/specs/consistency-review.md, CR-4), newest first: each
     check's verdict, the review that made it, leading to that review in its session, the
     revision whose material it covered, when, what it read, and its explanation, the
     reviewer's text, shown as text with each character a person may not see as its code
     point; and how many older checks the read leaves out. Only props, so a test renders
     it on the server. -->
<script lang="ts">
  import type { QuoteChecks } from '../lib/api-types';
  import { checkText, olderChecksText, VERDICT_WORDS } from '../lib/citations';
  import { reviewHref } from '../lib/router';
  import { visible } from '../lib/text';

  let {
    projectId,
    quote,
    loading,
  }: {
    projectId: string;
    /** The quote's checks as read; null until the read arrives. */
    quote: QuoteChecks | null;
    loading: boolean;
  } = $props();
</script>

{#if quote === null}
  {#if loading}<p class="muted">Reading its citation checks…</p>{/if}
{:else if quote.checks.length === 0}
  <p class="muted">No citation check of this quote yet.</p>
{:else}
  <p class="label">Its citation checks, newest first:</p>
  <ul class="citations">
    {#each quote.checks as check (check.check_id)}
      <li>
        <strong
          class:supported={check.verdict === 'supported'}
          class:unsupported={check.verdict === 'unsupported'}
          >{VERDICT_WORDS[check.verdict] ?? visible(String(check.verdict))}</strong
        >
        by
        <a href={reviewHref(projectId, check.session, check.review_id)}
          >review {visible(check.review_id)}</a
        >
        <span class="muted">({checkText(check)})</span>
        <details>
          <summary>Its explanation</summary>
          <p class="explanation">{visible(check.explanation)}</p>
        </details>
      </li>
    {/each}
  </ul>
  {#if olderChecksText(quote)}<p class="muted">{olderChecksText(quote)}</p>{/if}
{/if}

<style>
  .supported {
    color: var(--ok);
  }
  .unsupported {
    color: var(--failed);
  }
  .explanation {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .label {
    margin: 0.3em 0 0;
    font-size: 0.9em;
  }
</style>
