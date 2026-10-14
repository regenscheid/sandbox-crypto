<!-- SPDX-License-Identifier: MIT -->
<!-- The session's findings, from the reviews and the math checks. -->
<script lang="ts">
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
  const refresh = () => browser.refresh();
  const read = (review: string, evidence: string) => browser.read(review, evidence);
</script>

<h3>Findings</h3>
{#if !panel.findings.items.length}<p>No consistency findings recorded.</p>{/if}
{#each panel.findings.items as finding (finding.finding_id)}<FindingCard
    {projectId}
    {sessionId}
    {finding}
    changed={refresh}
    {read}
  />{/each}
{#if panel.findings.next}<button disabled={busy} onclick={() => browser.findings.more()}
    >Load more findings</button
  >{/if}
{#if panel.findings.error}<ErrorMessage error={panel.findings.error} />{/if}
