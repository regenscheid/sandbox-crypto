<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { RunTree } from '../lib/api-types';
  import { briefView, stateText } from '../lib/runTree';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { treeHref } from '../lib/router';
  import PollStatus from './PollStatus.svelte';
  import AgentActivity from './AgentActivity.svelte';
  let { projectId, sessionId }: { projectId: string; sessionId: string } = $props();
  let view = $state<PollState<RunTree>>(initialPoll());
  let selected = $state<string | null>(null);
  $effect(() => {
    const project = projectId,
      session = sessionId;
    const poller = new Poller(
      (signal) => api.runTree(project, session, signal),
      (value) => {
        view = value;
      },
      { intervalMs: 2000 },
    );
    return poller.watch();
  });
</script>

<section aria-label="Session agents">
  <h2>Agents</h2>
  <p class="muted">Follow the main agent and its delegated research.</p>
  <PollStatus phase={view.phase} hasData={view.data !== null} what="Agent activity" />
  {#if view.data}
    {#each view.data.runs as run (run.name)}
      {@const delegation = view.data.delegations.find((d) => d.delegation_id === run.delegation)}
      <article class="agent-card" class:agent-selected={selected === run.name}>
        <div class="agent-heading">
          <strong>{run.role === 'main' ? 'Main agent' : 'Research agent'}</strong><span
            class="state-badge"
            class:running={run.state === 'running'}>{stateText(run)}</span
          >
        </div>
        {#if delegation}<p class="agent-task">
            {briefView(delegation).task || briefView(delegation).note}
          </p>{/if}
        <p class="muted agent-id">
          {run.run_id}{#if run.parent}<br />Child of {run.parent}{/if}
        </p>
        <button
          onclick={() => {
            selected = selected === run.name ? null : run.name;
          }}
          aria-expanded={selected === run.name}
          >{selected === run.name ? 'Hide activity' : 'View activity'}</button
        >
      </article>
    {:else}<p class="empty-note">
        No agents have started yet. Their progress will appear here.
      </p>{/each}
    {#if view.data.delegations_total > view.data.delegations.length}<p class="muted">
        Showing the latest {view.data.delegations.length} of {view.data.delegations_total} delegations.
      </p>{/if}
  {/if}
  {#if selected}{#key selected}<AgentActivity {projectId} {sessionId} run={selected} />{/key}{/if}
  <a class="back-link" href={treeHref(projectId, sessionId)}>Open full run tree ↗</a>
</section>
