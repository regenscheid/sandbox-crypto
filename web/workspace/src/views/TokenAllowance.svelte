<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { onDestroy } from 'svelte';
  import type { TokenGrantDecision, TokenGrantStatus } from '../lib/api-types';
  import { get, post } from '../lib/http';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import ErrorMessage from './ErrorMessage.svelte';
  import PollStatus from './PollStatus.svelte';
  import TokenAllowance from './TokenAllowance.svelte';

  let { path, work, warningTotal }: { path: string; work: string; warningTotal: number | null } =
    $props();
  let pollState = $state<PollState<TokenGrantStatus>>(initialPoll());
  let refresh = $state(0);
  let reason = $state('Continue this research branch with 10,000,000 more tokens.');
  let busy = $state(false);
  let attempted = $state(false);
  let error = $state<unknown>(null);
  let initialTotal = $state<number | null>(null);
  let ancestorOpen = $state(false);
  let disposed = false;
  const controller = new AbortController();
  const grant = $derived(pollState.data);
  const baseline = $derived(warningTotal ?? initialTotal);
  const increased = $derived(grant !== null && baseline !== null && grant.total_tokens > baseline);

  onDestroy(() => {
    disposed = true;
    controller.abort();
  });

  $effect(() => {
    refresh;
    pollState = initialPoll();
    return new Poller(
      (signal) =>
        get<TokenGrantStatus>(`${path}/token-grants/${encodeURIComponent(work)}`, { signal }),
      (value) => {
        pollState = value;
        if (value.data !== null && initialTotal === null) initialTotal = value.data.total_tokens;
      },
      { once: true },
    ).watch();
  });

  async function approve() {
    if (
      busy ||
      attempted ||
      !grant ||
      grant.next_total_tokens === null ||
      increased ||
      !reason.trim()
    )
      return;
    // Pin the reviewed revision. A new read must never turn one click into another increment.
    const body = {
      work: grant.work,
      current: grant.current,
      epoch: grant.epoch,
      expected_revision: grant.revision,
      total_tokens: grant.next_total_tokens,
      reason: reason.trim(),
    };
    busy = true;
    attempted = true;
    error = null;
    try {
      await post<TokenGrantDecision>(`${path}/token-grants/next`, body, {
        signal: controller.signal,
        deadlineMs: 15_000,
      });
    } catch (caught) {
      if (!disposed) error = caught;
    } finally {
      if (!disposed) {
        busy = false;
        refresh++;
      }
    }
  }
</script>

<div class="allowance">
  <PollStatus phase={pollState.phase} hasData={grant !== null} what="Token allowance" />
  {#if error}<ErrorMessage {error} />{/if}
  {#if grant}
    <p>
      {grant.used_tokens.toLocaleString()} tokens used; cumulative allowance {grant.total_tokens.toLocaleString()}.
    </p>
    {#if increased}
      <p role="status">The allowance has increased. Resume research to continue.</p>
    {:else if attempted}
      <p role="status">
        Approval was sent. Check the recorded allowance before taking further action.
      </p>
    {:else if grant.blocked_reason}
      <p>{grant.blocked_reason}</p>
      {#if grant.blocked_ancestor_work}
        <button onclick={() => (ancestorOpen = !ancestorOpen)}>Review ancestor allowance</button>
        {#if ancestorOpen}
          <p class="muted">
            Review this ancestor's allowance first, then refresh the original branch.
          </p>
          {#key grant.blocked_ancestor_work}
            <TokenAllowance {path} work={grant.blocked_ancestor_work} warningTotal={null} />
          {/key}
        {/if}
      {/if}
    {:else if grant.next_total_tokens !== null}
      <label
        >Reason for continuing
        <textarea bind:value={reason} maxlength="2000" rows="2"></textarea>
      </label>
      <p>
        This approval adds exactly 10,000,000 tokens, for {grant.next_total_tokens.toLocaleString()} total.
        Spending controls still apply.
      </p>
      <button disabled={busy || !reason.trim()} onclick={approve}
        >Approve 10,000,000 more tokens</button
      >
    {/if}
  {/if}
  <button class="subtle" disabled={busy || pollState.busy} onclick={() => refresh++}
    >Refresh allowance</button
  >
</div>

<style>
  .allowance {
    margin-top: 0.65rem;
    padding: 0.75rem;
    border-left: 2px solid var(--border);
  }
  label {
    display: grid;
    gap: 0.35rem;
  }
  textarea {
    width: 100%;
    box-sizing: border-box;
  }
  button {
    margin: 0.35rem 0.5rem 0 0;
  }
</style>
