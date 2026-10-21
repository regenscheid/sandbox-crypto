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
<!-- While research is active the header says so and there is nothing to resume, so the
     conversation shows nothing here (owner, 2026-10-21). -->
{#if !closed && !recovery?.active && (held || (recovery && recovery.unfinished > 0))}
  <section class="stopped-card" aria-label="Research stopped">
    <strong>Research stopped before it finished</strong>
    <p role="status">
      {#if recovery && recovery.unfinished > 0}
        {recovery.unfinished === 1
          ? 'One line of research'
          : `${recovery.unfinished} lines of research`} stopped before finishing. The plan, files and
        finished checks are kept; resuming picks up from the last saved step.
      {:else}
        Your messages are held until you resume.
      {/if}
    </p>
    {#each blocked as work (work.work)}
      <p class="muted">Blocked: {work.reason ?? 'Review this work before resuming.'}</p>
    {/each}
    <div class="actions">
      <button class="primary-button" disabled={busy} onclick={resume}>Resume research</button>
    </div>
  </section>
{/if}
