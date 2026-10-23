<!-- SPDX-License-Identifier: MIT -->
<!-- The session's agents from its run tree: those working now, then those that finished,
     each in words (what it was asked, how it ended) rather than its record. "View activity"
     shows a run's events, "Context management" what its model was given; the full run tree
     has every field. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { browserState } from '../lib/browserState';
  import type { Delegation, RunTree, TreeRun } from '../lib/api-types';
  import { clock, firstLine } from '../lib/conversation';
  import { agentName, briefView, endedText, resultView } from '../lib/runTree';
  import { treeHref } from '../lib/router';
  import { visible } from '../lib/text';
  import AgentActivity from './AgentActivity.svelte';
  import SavedContext from './SavedContext.svelte';
  let {
    projectId,
    sessionId,
    tree,
  }: { projectId: string; sessionId: string; tree: RunTree | null } = $props();
  const scope = untrack(() => `agents:${projectId}:${sessionId}`);
  const kept = untrack(() => browserState.load(scope).draft ?? {});
  let selected = $state<string | null>(kept.selected || null);
  $effect(() => {
    browserState.save(scope, { selected: selected ?? '' });
  });
  const delegation = (run: TreeRun) =>
    tree?.delegations.find((item) => item.delegation_id === run.delegation) ?? null;
  const runs = $derived([...(tree?.runs ?? [])].reverse());
  const running = $derived(runs.filter((run) => run.state === 'running'));
  const finished = $derived(runs.filter((run) => run.state !== 'running'));

  function summary(run: TreeRun, record: Delegation | null): string {
    if (record === null) {
      if (run.state === 'interrupted') return 'Interrupted: its process ended.';
      if (run.stopped === 'answered') return 'Answered.';
      return run.stopped === null
        ? 'Ended.'
        : `Stopped: ${visible(run.stopped.replaceAll('_', ' '))}.`;
    }
    const result = resultView(record);
    return result?.summary ? firstLine(result.summary, 280) : endedText(record);
  }
</script>

{#snippet card(run: TreeRun, live: boolean)}
  {@const record = delegation(run)}
  <article class="agent-card" class:finished={!live} class:agent-selected={selected === run.name}>
    <div class="agent-heading">
      {#if live}<span class="session-dot" aria-hidden="true"></span>{/if}
      <strong>{agentName(run, record)}</strong>
      <span class="muted"
        >{live
          ? `since ${clock(run.started_at)}`
          : record?.ended_at
            ? `${clock(run.started_at)}–${clock(record.ended_at)}`
            : clock(run.started_at)}</span
      >
    </div>
    {#if live}
      {#if record && run.role !== 'main' && record.kind === 'task' && briefView(record).note}<p
          class="agent-task"
        >
          {firstLine(briefView(record).note, 200)}
        </p>{/if}
    {:else}<p class="agent-task">{summary(run, record)}</p>{/if}
    <div class="agent-detail">
      <span
        >{run.alias ? visible(run.alias) : 'Default model'} · {run.role === 'main'
          ? 'main agent'
          : record?.kind === 'check'
            ? 'independent check'
            : 'subagent'}</span
      >
      <button
        type="button"
        class="link-button"
        aria-expanded={selected === run.name}
        onclick={() => {
          selected = selected === run.name ? null : run.name;
        }}>{selected === run.name ? 'Hide activity' : 'View activity'}</button
      >
      <!-- What the agent's model was given, and how its context was folded. -->
      <SavedContext
        project={projectId}
        session={sessionId}
        run={run.name}
        compact
        scope={run.role === 'main' ? 'agent' : 'attempt'}
      />
    </div>
  </article>
{/snippet}

<section aria-label="Session agents">
  {#if tree}
    <h2>Working now</h2>
    {#each running as run (run.name)}{@render card(run, true)}{:else}<p class="muted">
        No agent is working now.
      </p>{/each}
    {#if finished.length}
      <h2>Finished</h2>
      {#each finished as run (run.name)}{@render card(run, false)}{/each}
    {/if}
    {#if tree.delegations_total > tree.delegations.length}<p class="muted">
        Showing the latest {tree.delegations.length} of {tree.delegations_total} delegations.
      </p>{/if}
  {:else}
    <p class="muted">Reading the session's agents…</p>
  {/if}
  {#if selected}{#key selected}<div class="agent-activity">
        <AgentActivity {projectId} {sessionId} run={selected} />
      </div>{/key}{/if}
  <a class="back-link" href={treeHref(projectId, sessionId)}>Open the full run tree ↗</a>
</section>
