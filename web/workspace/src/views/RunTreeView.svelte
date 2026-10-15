<!-- SPDX-License-Identifier: MIT -->
<!-- A session's run tree (docs/specs/plans-and-delegation.md, PD-42): its runs, running
     ones included, nested under the runs that delegated to them, each with its role,
     state, model and limits, leading to its events; each delegation's brief, read-only,
     and outcome; and the delegations the read lists whose runs it does not. Read on
     request again. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { RunTree } from '../lib/api-types';
  import { Detail, noDetail } from '../lib/detail';
  import { projectHref, sessionHref } from '../lib/router';
  import { delegationsById, runForest } from '../lib/runTree';
  import DelegationCard from './DelegationCard.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  import Node from './RunTreeNode.svelte';

  let { projectId, sessionId }: { projectId: string; sessionId: string } = $props();
  const read = $state(noDetail<string, RunTree>());
  // The route keys this view by the project and session: they never change here.
  const tree = new Detail(read, () => api.runTree(projectId, sessionId));
  void tree.show(untrack(() => `${projectId}/${sessionId}`));
  const forest = $derived(read.value === null ? [] : runForest(read.value.runs));
  const delegations = $derived(delegationsById(read.value?.delegations ?? []));
  /** Delegations whose runs the read does not list, shown apart. */
  const apart = $derived(
    (read.value?.delegations ?? []).filter(
      (delegation) => !read.value!.runs.some((run) => run.delegation === delegation.delegation_id),
    ),
  );
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
  <a href={sessionHref(projectId, sessionId)}>{sessionId}</a> /
</nav>
<h1>Run tree</h1>
<p class="muted">
  The session's runs, each under the run that delegated to it, with its delegation's brief and
  outcome. The brief and the limits are shown as they were granted, read-only.
  <button type="button" disabled={read.loading} onclick={() => tree.reload()}>Read again</button>
</p>
{#if read.value}
  {#if forest.length}
    <ul class="tree">
      {#each forest as node (node.run.name)}
        <Node {projectId} {sessionId} {node} {delegations} />
      {/each}
    </ul>
  {:else}
    <p class="muted">The session has no runs yet.</p>
  {/if}
  {#if apart.length}
    <h2>Delegations whose runs are not listed</h2>
    {#each apart as delegation (delegation.delegation_id)}
      <DelegationCard {projectId} {sessionId} {delegation} />
    {/each}
  {/if}
  {#if read.value.delegations_total > read.value.delegations.length}
    <p class="muted">
      The latest {read.value.delegations.length} of the session's {read.value.delegations_total} delegations
      are listed.
    </p>
  {/if}
{/if}
{#if read.loading}<p class="muted">Loading…</p>{/if}
{#if read.error}<ErrorMessage error={read.error} />{/if}
