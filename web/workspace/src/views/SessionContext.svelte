<!-- SPDX-License-Identifier: MIT -->
<!-- The session's Context tab: what each agent's model is given, one click from the session.
     The research agent's latest run comes first; every other run of the session follows,
     newest first. Each opens the context inspector: its call timeline, the folds that
     shortened its history, its current context and the original history. The research
     agent's opens on its context across continuations: a resumed run may capture nothing
     of its own. -->
<script lang="ts">
  import type { RunTree, TreeRun } from '../lib/api-types';
  import { clock } from '../lib/conversation';
  import { agentName } from '../lib/runTree';
  import { visible } from '../lib/text';
  import SavedContext from './SavedContext.svelte';
  let {
    projectId,
    sessionId,
    tree,
  }: { projectId: string; sessionId: string; tree: RunTree | null } = $props();
  const runs = $derived([...(tree?.runs ?? [])].reverse());
  const main = $derived(runs.find((run) => run.role === 'main') ?? null);
  const others = $derived(runs.filter((run) => run !== main));
  const name = (run: TreeRun) =>
    agentName(run, tree?.delegations.find((item) => item.delegation_id === run.delegation) ?? null);
  const when = (run: TreeRun) =>
    run.state === 'running'
      ? `working since ${clock(run.started_at)}`
      : `started ${clock(run.started_at)}`;
</script>

<section aria-label="Agent context" class="context-panel">
  <p class="muted intro">
    What each agent's model is given: its calls over time, the folds that shortened its history, its
    context now, and the original history.
  </p>
  {#if tree === null}
    <p class="muted">Reading the session's agents…</p>
  {:else if main === null}
    <p class="muted">No agent has run in this session yet.</p>
  {:else}
    <div class="main-agent">
      <div>
        <strong>Research agent</strong>
        <span class="muted">{when(main)} · <code>{visible(main.name)}</code></span>
      </div>
      <SavedContext
        project={projectId}
        session={sessionId}
        run={main.name}
        primary
        label="Open its context"
        tab="context"
        scope="agent"
      />
    </div>
    {#if others.length}
      <h3 class="eyebrow">Other runs</h3>
      <ul class="runs">
        {#each others as run (run.name)}
          <li>
            <span class="run-name">{visible(name(run))}</span>
            <span class="muted">{when(run)}</span>
            <SavedContext
              project={projectId}
              session={sessionId}
              run={run.name}
              compact
              label="Context"
              scope={run.role === 'main' ? 'agent' : 'attempt'}
            />
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>

<style>
  .intro {
    margin: 0 0 14px;
    font-size: 13px;
  }
  .main-agent {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 12px 14px;
    border: 1px solid var(--border);
    border-radius: 10px;
  }
  .main-agent div {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .main-agent .muted {
    font-size: 12px;
  }
  h3 {
    margin: 20px 0 6px;
  }
  .runs {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .runs li {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 10px;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
  }
  .run-name {
    flex: 1 1 12rem;
    min-width: 0;
  }
  .runs .muted {
    font-size: 12px;
  }
</style>
