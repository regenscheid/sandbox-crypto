<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import type { PollPhase } from '../lib/poller';

  let { phase, hasData, what }: { phase: PollPhase; hasData: boolean; what: string } = $props();
  const earlier = $derived(hasData ? ' What is shown is from an earlier read.' : '');
</script>

{#if phase === 'loading' && !hasData}
  <p class="muted" role="status">Loading…</p>
{:else if phase === 'paused'}
  <p class="muted" role="status">{what} paused while this page is hidden.</p>
{:else if phase === 'unavailable'}
  <p class="problem" role="status">{what} unavailable.{earlier}</p>
{:else if phase === 'unsupported'}
  <p class="problem" role="status">{what} need a configured worker connection.</p>
{:else if phase === 'changed'}
  <p class="problem" role="status">{what} stopped: the record changed. Reload the page.</p>
{:else if phase === 'missing'}
  <p class="problem" role="status">This session or project is not recorded.</p>
{:else if phase === 'signed_out'}
  <p class="problem" role="status">
    Not logged in. Reload the page to sign in through Authentik, or open the login URL that
    <code>sandbox-science-workspace serve</code> printed when it started.
  </p>
{/if}
