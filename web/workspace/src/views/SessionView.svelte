<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api } from '../lib/api';
  import { formatTime } from '../lib/format';
  import { projectHref } from '../lib/router';
  import { kernelPanelScope } from '../lib/kernels';
  import ErrorMessage from './ErrorMessage.svelte';
  import JobsPanel from './JobsPanel.svelte';
  import KernelPanel from './KernelPanel.svelte';
  import RunSection from './RunSection.svelte';
  const reviewComponent = import('./ReviewsPanel.svelte');

  let { projectId, sessionId }: { projectId: string; sessionId: string } = $props();
  const loading = $derived(api.session(projectId, sessionId));
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
</nav>
<h1>Session {sessionId}</h1>

{#await loading}
  <p class="muted">Loading…</p>
{:then detail}
  <dl class="facts">
    <dt>Environment</dt>
    <dd>
      <code>{detail.session.environment_id}</code>
      <span class="muted">
        {detail.session.dedicated ? 'its own' : "the project's default, shared with its sessions"}
      </span>
    </dd>
    <dt>Opened</dt>
    <dd>{formatTime(detail.session.opened_at)}</dd>
    <dt>State</dt>
    <dd>
      {detail.session.closed_at ? `closed ${formatTime(detail.session.closed_at)}` : 'open'}
    </dd>
  </dl>

  {@const kernelScope = kernelPanelScope(projectId, sessionId, detail)}
  {#if kernelScope}
    <KernelPanel {projectId} {sessionId} environmentId={kernelScope.environmentId} />
  {/if}
  <JobsPanel {projectId} {sessionId} />
  {#await reviewComponent then component}
    {@const Reviews = component.default}
    <Reviews {projectId} {sessionId} />
  {/await}

  <h2>Runs</h2>
  {#if detail.runs.length === 0}
    <p>
      No runs kept. The harness keeps a run when it runs with <code>--state-dir</code>.
    </p>
  {:else}
    <p class="muted">Oldest first. The newest is open; open the others to read them.</p>
    {#each detail.runs as run, index (run.name)}
      <RunSection {projectId} {sessionId} {run} initiallyOpen={index === detail.runs.length - 1} />
    {/each}
  {/if}
{:catch error}
  <ErrorMessage {error} />
{/await}
