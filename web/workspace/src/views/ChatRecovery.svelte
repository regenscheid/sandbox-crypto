<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import type { StatusPage } from '../lib/api-types';
  import { get } from '../lib/http';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import PollStatus from './PollStatus.svelte';
  import ChatResources from './ChatResources.svelte';

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
{#key `${projectId}/${sessionId}`}<ChatResources {projectId} {sessionId} />{/key}
{#if recovery && recovery.unfinished > 0}
  {@const branches = recovery.unfinished === 1 ? 'branch' : 'branches'}
  <p role="status">
    {#if recovery.active}
      The agent is working on {recovery.unfinished === 1 ? 'the' : recovery.unfinished} open research
      {branches}.
    {:else}
      {recovery.unfinished} unfinished research {branches}, stopped: resume to continue.
    {/if}
  </p>
  {#each blocked as work (work.work)}
    <p class="muted">Blocked: {work.reason ?? 'Review this branch before resuming.'}</p>
  {/each}
{/if}
<!-- While research is active there is nothing to resume, so no button (owner, 2026-10-21). -->
{#if !closed && !recovery?.active && (held || (recovery && recovery.unfinished > 0))}
  <button disabled={busy} onclick={resume}>Resume research</button>
{/if}
