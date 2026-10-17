<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import type { StatusPage } from '../lib/api-types';
  import { get } from '../lib/http';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import PollStatus from './PollStatus.svelte';

  let {
    projectId,
    sessionId,
    held,
    busy,
    closed,
    resume,
  }: {
    projectId: string;
    sessionId: string;
    held: boolean;
    busy: boolean;
    closed: boolean;
    resume: () => Promise<void>;
  } = $props();
  let state = $state<PollState<StatusPage>>(initialPoll());
  const recovery = $derived(state.data);
  const blocked = $derived(recovery?.works.filter((work) => work.state === 'blocked') ?? []);

  $effect(() => {
    const path = `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/recovery`;
    state = initialPoll();
    return new Poller(
      (signal) => get<StatusPage>(path, { signal }),
      (value) => {
        state = value;
      },
      { intervalMs: 3000 },
    ).watch();
  });
</script>

<PollStatus phase={state.phase} hasData={recovery !== null} what="Research recovery" />
{#if recovery && recovery.unfinished > 0}
  <p role="status">
    {recovery.unfinished} unfinished research {recovery.unfinished === 1 ? 'branch' : 'branches'}.
    {#if recovery.active}Research is active.{/if}
  </p>
  {#each blocked as work (work.work)}
    <p class="muted">Blocked: {work.reason ?? 'Review this branch before resuming.'}</p>
  {/each}
{/if}
{#if !closed && (held || (recovery && recovery.unfinished > 0))}
  <button disabled={busy || recovery?.active} onclick={resume}>Resume research</button>
{/if}
