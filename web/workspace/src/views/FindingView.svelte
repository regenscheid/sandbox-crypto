<!-- SPDX-License-Identifier: MIT -->
<!-- One review finding in its session, as a ledger entry links to it (docs/specs/ledger.md,
     LG-25, LG-26): the finding's card, as the session's review panel shows it. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { Detail, noDetail } from '../lib/detail';
  import { reviews, type Finding } from '../lib/reviews';
  import { projectHref, sessionHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import FindingCard from './FindingCard.svelte';

  let {
    projectId,
    sessionId,
    findingId,
  }: { projectId: string; sessionId: string; findingId: string } = $props();
  const shown = $state(noDetail<string, Finding>());
  const detail = new Detail(shown, (id) => reviews.finding(projectId, sessionId, id));
  // The route keys this view by the finding: its ID never changes here.
  void detail.show(untrack(() => findingId));
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
  <a href={sessionHref(projectId, sessionId)}>{sessionId}</a> /
</nav>
<h1>Review finding</h1>
{#if shown.value}
  <FindingCard {projectId} {sessionId} finding={shown.value} changed={() => detail.reload()} />
{:else if shown.loading}
  <p class="muted">Loading…</p>
{/if}
{#if shown.error}<ErrorMessage error={shown.error} />{/if}
