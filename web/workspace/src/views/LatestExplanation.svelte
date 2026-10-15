<!-- SPDX-License-Identifier: MIT -->
<!-- The latest explanation an agent gave on a finding, a reviewer's or the main agent's,
     exactly as agents read it, where the user confirms, dismisses or rebuts it (threat
     model, A3): its reason and the explanation source it wrote. Only props, so a test
     renders it on the server. -->
<script lang="ts">
  import { actorText } from '../lib/findingView';
  import type { Transition } from '../lib/reviews';
  import ApprovalText from './ApprovalText.svelte';

  let { entries, loading = false }: { entries: Transition[]; loading?: boolean } = $props();
  /** An agent's step: a review's, or a session's main agent. */
  const latest = $derived(entries.findLast((entry) => /^(review|session):/.test(entry.actor)));
</script>

{#if latest}
  <ApprovalText
    text={latest.reason}
    label={`The latest explanation, by ${actorText(latest.actor)} (revision ${latest.revision}), exactly`}
  />
  {#if latest.response_body}<ApprovalText
      text={latest.response_body.body}
      label="Its explanation source, exactly"
    />{/if}
{:else if loading}
  <p role="status">Reading the latest explanation…</p>
{:else if entries.length}
  <p class="muted">No agent has explained this finding yet.</p>
{/if}
