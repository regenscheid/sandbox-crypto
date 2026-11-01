<!-- SPDX-License-Identifier: MIT -->
<!--
  The session's findings, from the reviews and the math checks: the unresolved ones first,
  then those fixed, dismissed or superseded, folded (CR-41), among the findings loaded so far.
  One keyed list, so a card that changes group keeps its state, a save in progress included.
-->
<script lang="ts">
  import { announceFindingChange } from '../lib/findingRows';
  import { resolved } from '../lib/findingView';
  import type { ReviewBrowser, ReviewPanel } from '../lib/reviewBrowser';
  import ErrorMessage from './ErrorMessage.svelte';
  import FindingCard from './FindingCard.svelte';
  let {
    projectId,
    sessionId,
    panel,
    browser,
    busy,
  }: {
    projectId: string;
    sessionId: string;
    panel: ReviewPanel;
    browser: ReviewBrowser;
    busy: boolean;
  } = $props();
  // The conversation's finding rows read the session's findings again too (B10, item 4).
  const refresh = () => {
    announceFindingChange(projectId, sessionId);
    return browser.refresh();
  };
  // Unresolved first; the sort is stable, so each group keeps the server's order.
  const ordered = $derived(
    [...panel.findings.items].sort(
      (a, b) => Number(resolved(a.status)) - Number(resolved(b.status)),
    ),
  );
  const closed = $derived(ordered.filter((item) => resolved(item.status)).length);
  const reviewOf = (id: string | null) =>
    id === null ? undefined : panel.reviews.items.find((item) => item.review_id === id);
</script>

<h3>Findings</h3>
{#if !panel.findings.items.length && !panel.findings.loading && panel.findings.error === null}<p>
    No consistency findings recorded.
  </p>{/if}
{#if panel.findings.items.length && closed === panel.findings.items.length}<p>
    No unresolved findings.
  </p>{/if}
{#if closed}<p class="muted">
    {closed} fixed, dismissed or superseded, folded below the unresolved ones.
  </p>{/if}
{#each ordered as finding (finding.finding_id)}<FindingCard
    {projectId}
    {sessionId}
    {finding}
    review={reviewOf(finding.review_id)}
    folded={resolved(finding.status)}
    changed={refresh}
  />{/each}
{#if panel.findings.next}<button disabled={busy} onclick={() => browser.findings.more()}
    >Load more findings</button
  >{/if}
{#if panel.findings.error}<ErrorMessage error={panel.findings.error} />{/if}
