<!-- SPDX-License-Identifier: MIT -->
<!--
  One reviewer finding in the conversation (B10, item 4; CR-41, CR-46), compact: its status
  chip, severity and category, the reviewer's statement as text with every hidden character
  as its code point, its anchors in words with links to the document block or the ledger
  entry, the successor that replaced it, the latest re-check's answer, and the full card in
  the session's review panel. It shows the finding as last read, so a status set after the
  notice (a re-check, the author or the user) shows here too.
-->
<script lang="ts">
  import { findingRow, type FindingNotice } from '../lib/findingRows';
  import { findingHref } from '../lib/router';
  import type { Finding } from '../lib/reviews';
  import { visible } from '../lib/text';

  let {
    projectId,
    sessionId,
    notice,
    finding = null,
    failed = null,
  }: {
    projectId: string;
    sessionId: string;
    /** The finding as the notice offered it. */
    notice: FindingNotice;
    /** The finding as last read, once it was. */
    finding?: Finding | null;
    /** Why it could not be read, when it could not. */
    failed?: string | null;
  } = $props();
  const row = $derived(finding ? findingRow(projectId, sessionId, finding, notice.revision) : null);
</script>

<article class="finding-row" aria-label={`Reviewer finding ${visible(notice.finding)}`}>
  {#if row}
    <header>
      <span class={`finding-chip ${row.chipClass}`}>{row.chip}</span>
      <strong>{row.severity}</strong>
      <span class="muted">· {row.category}</span>
      <a class="finding-card-link" href={row.card}>Full card</a>
    </header>
    <p class="finding-statement" title="The reviewer's words, shown as text">
      <span class="finding-says">reviewer says</span>
      {row.statement}
    </p>
    {#if row.anchors.length}<ul class="finding-anchors" aria-label="Where">
        {#each row.anchors as anchor, index (index)}<li>
            {#if anchor.href}<a href={anchor.href}>{anchor.text}</a>{:else}{anchor.text}{/if}
          </li>{/each}
        {#if row.moreAnchors}<li class="muted">and {row.moreAnchors} more on the full card</li>{/if}
      </ul>{/if}
    {#if row.recheck}<p class="finding-recheck">{row.recheck}</p>{/if}
    {#if row.successor}<p>
        Replaced by <a href={row.successor.href}><code>{row.successor.id}</code></a>
      </p>{/if}
    {#if row.revised}<p class="muted">
        Offered at revision {notice.revision}; now at revision {finding!.revision}.
      </p>{/if}
  {:else}
    <header>
      <span class="finding-chip status-unknown">Finding</span>
      <code>{visible(notice.finding)}</code>
      <a class="finding-card-link" href={findingHref(projectId, sessionId, notice.finding)}
        >Full card</a
      >
    </header>
    <p class="muted" role={failed ? 'alert' : 'status'}>
      {failed ? `It could not be read: ${failed}` : 'Reading the finding…'}
    </p>
  {/if}
</article>

<style>
  .finding-row {
    border: 1px solid var(--border);
    border-left: 3px solid var(--warn-text, var(--border));
    border-radius: 8px;
    background: var(--surface);
    padding: 8px 12px;
    font-size: 13px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .finding-row p {
    margin: 0;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px;
  }
  .finding-card-link {
    margin-left: auto;
    font-size: 12px;
  }
  .finding-chip {
    border: 1px solid currentColor;
    border-radius: 9px;
    padding: 0 8px;
    font-size: 12px;
  }
  .status-open {
    font-weight: 600;
    color: var(--warn-text, inherit);
  }
  .status-fixed {
    color: var(--ok-text, inherit);
  }
  .status-dismissed {
    font-style: italic;
    color: var(--muted);
  }
  .status-superseded {
    color: var(--muted);
  }
  .status-stale {
    border-style: dashed;
    color: var(--muted);
  }
  .finding-statement {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .finding-says {
    font-size: 11px;
    font-style: italic;
    color: var(--muted);
    margin-right: 4px;
  }
  .finding-anchors {
    margin: 0;
    padding-left: 18px;
    font-size: 12px;
  }
  .finding-recheck {
    font-size: 12px;
    color: var(--muted);
  }
</style>
