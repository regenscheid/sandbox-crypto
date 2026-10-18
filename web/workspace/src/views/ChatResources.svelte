<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import type { ResourceRequest, ResourceRequestPage } from '../lib/api-types';
  import { get } from '../lib/http';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { messageHref } from '../lib/router';
  import PollStatus from './PollStatus.svelte';
  import TokenAllowance from './TokenAllowance.svelte';

  let { projectId, sessionId }: { projectId: string; sessionId: string } = $props();
  let pollState = $state<PollState<ResourceRequestPage>>(initialPoll());
  let after = $state('');
  let selected = $state<string | null>(null);
  const path = $derived(
    `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/recovery`,
  );
  const page = $derived(pollState.data);
  const current = (item: ResourceRequest) =>
    !item.superseded && !['completed', 'stopped'].includes(item.work_state);

  $effect(() => {
    const cursor = after;
    selected = null;
    pollState = initialPoll();
    return new Poller(
      (signal) =>
        get<ResourceRequestPage>(`${path}/resources?after=${encodeURIComponent(cursor)}`, {
          signal,
        }),
      (value) => {
        pollState = value;
      },
      { intervalMs: 5000 },
    ).watch();
  });
</script>

<PollStatus phase={pollState.phase} hasData={page !== null} what="Research check-ins" />
{#if page && (page.requests.length > 0 || after)}
  <details
    class="resources"
    open={page.requests.some(
      (item) => current(item) && !item.decision && item.user_grant_revision === null,
    )}
  >
    <summary>Research check-ins</summary>
    <p class="muted">
      Agents ask their parents to review progress before reaching an allowance. Token increases
      require your approval.
    </p>
    <ul>
      {#each page.requests as item (item.request_id)}
        <li>
          <strong>{item.task || 'Research branch'}</strong>
          <p>
            {item.used.toLocaleString()} of {item.total.toLocaleString()}
            {item.resource} at check-in.
          </p>
          {#if item.user_grant_revision !== null}
            <p>Token extension recorded by the user.</p>
          {:else if item.decision}
            <p>
              Parent decision: {item.decision.request.action.replaceAll('_', ' ')}. {item.decision
                .request.reason}
            </p>
          {:else if !current(item)}
            <p class="muted">
              Historical check-in · {item.superseded
                ? 'a newer attempt is recorded'
                : item.work_state}.
            </p>
          {:else}
            <p class="muted">Awaiting review.</p>
          {/if}
          {#if item.checkpoint_message_id}
            <a href={messageHref(projectId, item.checkpoint_message_id)}>Read checkpoint</a>
          {/if}
          {#if current(item) && item.resource === 'tokens' && item.parent && item.user_grant_revision === null}
            <button
              class="subtle"
              onclick={() => (selected = selected === item.request_id ? null : item.request_id)}
              >Review token allowance</button
            >
            {#if selected === item.request_id}
              <TokenAllowance {path} work={item.work} warningTotal={item.total} />
            {/if}
          {/if}
        </li>
      {/each}
    </ul>
    {#if after}<button onclick={() => (after = '')}>Latest check-ins</button>{/if}
    {#if page.next_after}<button onclick={() => (after = page.next_after ?? '')}
        >Older check-ins</button
      >{/if}
  </details>
{/if}

<style>
  .resources {
    margin: 0.75rem 0;
    padding: 0.75rem;
    border: 1px solid var(--border);
    border-radius: 0.5rem;
  }
  summary {
    cursor: pointer;
    font-weight: 600;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  li {
    padding: 0.75rem 0;
    border-top: 1px solid var(--border);
  }
  p {
    margin: 0.4rem 0;
  }
  li > button {
    margin-left: 0.5rem;
  }
</style>
