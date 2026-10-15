<!-- SPDX-License-Identifier: MIT -->
<!-- One run of a session's tree, and the runs it delegated to, nested (docs/specs/
     plans-and-delegation.md, PD-42): its role, state, model and the limits it ran under,
     a link to its events, and for a delegated run its delegation. Only props, so a test
     renders it on the server. -->
<script lang="ts">
  import type { Delegation } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { runHref } from '../lib/router';
  import { limitsText, stateText, type TreeNode } from '../lib/runTree';
  import { visible } from '../lib/text';
  import DelegationCard from './DelegationCard.svelte';
  import Self from './RunTreeNode.svelte';

  let {
    projectId,
    sessionId,
    node,
    delegations,
  }: {
    projectId: string;
    sessionId: string;
    node: TreeNode;
    delegations: Map<string, Delegation>;
  } = $props();
  const run = $derived(node.run);
  const delegation = $derived(
    run.delegation === null ? undefined : delegations.get(run.delegation),
  );
  const limits = $derived(limitsText(run.limits));
</script>

<li class="tree-run">
  <p>
    <strong>{run.role === 'main' ? 'Main run' : 'Delegated run'}</strong>
    <a href={runHref(projectId, sessionId, run.name)}>{visible(run.run_id)}</a>:
    <span class:running={run.state === 'running'} class:problem={run.state === 'interrupted'}
      >{stateText(run)}</span
    >
    <span class="muted"
      >· started {formatTime(run.started_at)} · model {run.alias
        ? visible(run.alias)
        : 'not recorded yet'}{limits ? ` · limits ${limits}` : ''}{run.kept
        ? ''
        : ' · record not kept yet'}</span
    >
  </p>
  {#if run.delegation !== null}
    {#if delegation}
      <DelegationCard {projectId} {sessionId} runId={run.run_id} {delegation} />
    {:else}
      <p class="muted">
        Its delegation <code>{visible(run.delegation)}</code> is older than those the read lists.
      </p>
    {/if}
  {/if}
  {#if node.children.length}
    <ul class="tree">
      {#each node.children as child (child.run.name)}
        <Self {projectId} {sessionId} node={child} {delegations} />
      {/each}
    </ul>
  {/if}
</li>

<style>
  .tree-run p {
    margin: 0.2em 0;
  }
  .running {
    color: var(--ok);
  }
</style>
