<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { focusPanel } from '../lib/focusPanel';
  import { untrack } from 'svelte';
  import { api, type SessionDetail } from '../lib/api';
  import { formatTime } from '../lib/format';
  import SessionDocuments from './SessionDocuments.svelte';
  import SessionAgents from './SessionAgents.svelte';
  import PaneResizer from './PaneResizer.svelte';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { projectHref } from '../lib/router';
  import { kernelPanelScope } from '../lib/kernels';
  import { rememberSession } from '../lib/lastSession';
  import ChatPanel from './ChatPanel.svelte';
  import FoldProposals from './FoldProposals.svelte';
  import JobsPanel from './JobsPanel.svelte';
  import KernelPanel from './KernelPanel.svelte';
  import PlanPanel from './PlanPanel.svelte';
  import PollStatus from './PollStatus.svelte';
  import RunSection from './RunSection.svelte';
  const reviewComponent = import('./ReviewsPanel.svelte');

  let {
    projectId,
    sessionId,
    reviewId = null,
  }: {
    projectId: string;
    sessionId: string;
    /** A review to choose in the review panel, as a link names it. */
    reviewId?: string | null;
  } = $props();
  type Panel = 'documents' | 'plan' | 'agents' | 'activity';
  let panel = $state<Panel | null>(
    untrack(() => (reviewId ? 'activity' : window.innerWidth > 1120 ? 'plan' : null)),
  );
  let visited = $state<Panel[]>(untrack(() => (reviewId ? ['activity'] : ['plan'])));
  let width = $state(460);
  let viewportWidth = $state(window.innerWidth);
  const narrow = $derived(viewportWidth <= 1120);
  const maxWidth = $derived(Math.max(320, Math.floor(viewportWidth / 2)));
  function closePane() {
    const id = panel;
    panel = null;
    queueMicrotask(() =>
      document
        .querySelector<HTMLButtonElement>(`.research-actions button[data-panel="${id}"]`)
        ?.focus(),
    );
  }
  const tabs: { id: Panel; label: string }[] = [
    { id: 'documents', label: 'Documents' },
    { id: 'plan', label: 'Plan' },
    { id: 'agents', label: 'Agents' },
    { id: 'activity', label: 'Activity' },
  ];
  function choose(id: Panel) {
    if (!visited.includes(id)) visited = [...visited, id];
    panel = panel === id ? null : id;
  }
  // Polled, so that a run that ends and a session that closes show up.
  let view = $state<PollState<SessionDetail>>(initialPoll());
  $effect(() => {
    const project = projectId,
      session = sessionId;
    rememberSession(project, session);
    view = initialPoll();
    const poller = new Poller(
      (signal) => api.session(project, session, signal),
      (value) => {
        view = value;
      },
    );
    return poller.watch();
  });
</script>

<svelte:window
  onresize={() => {
    viewportWidth = window.innerWidth;
    width = Math.min(width, maxWidth);
  }}
/>
<div class="session-workspace" style:--inspector-width={`${width}px`}>
  <header class="session-header" inert={narrow && panel !== null}>
    <div>
      <span class="eyebrow">Research session</span>
      <h1>{sessionId}</h1>
    </div>
    <div class="research-actions" aria-label="Research views">
      {#each tabs as tab}<button
          data-panel={tab.id}
          class:active={panel === tab.id}
          aria-pressed={panel === tab.id}
          aria-controls="research-pane"
          onclick={() => choose(tab.id)}>{tab.label}</button
        >{/each}
    </div>
  </header>
  <div class="session-columns">
    <div class="conversation-column" inert={narrow && panel !== null}>
      <PollStatus phase={view.phase} hasData={view.data !== null} what="Session views" />
      {#if view.data}
        <ChatPanel {projectId} {sessionId} closed={view.data.session.closed_at !== null} />
      {/if}
    </div>
    {#if panel}<PaneResizer bind:width {maxWidth} />{/if}
    <aside
      id="research-pane"
      class="research-pane"
      hidden={!panel}
      aria-label="Research pane"
      use:focusPanel={{ active: narrow && panel !== null, close: closePane }}
      role={narrow ? 'dialog' : undefined}
      aria-modal={narrow && panel !== null ? true : undefined}
      tabindex="-1"
    >
      <div class="pane-heading">
        <span class="eyebrow">{tabs.find((tab) => tab.id === panel)?.label}</span>
        {#if !narrow}<button
            class="pane-expand"
            title="Expand to half the window"
            aria-label="Expand research pane to half the window"
            onclick={() => (width = maxWidth)}>↔</button
          >{/if}<button class="icon-button" aria-label="Close research pane" onclick={closePane}
          >×</button
        >
      </div>
      <div class="pane-content">
        {#if visited.includes('documents')}<div hidden={panel !== 'documents'}>
            <SessionDocuments {projectId} {sessionId} />
          </div>{/if}
        {#if visited.includes('agents')}<div hidden={panel !== 'agents'}>
            <SessionAgents {projectId} {sessionId} />
          </div>{/if}
        {#if view.data}
          {@const detail = view.data}
          {@const kernelScope = kernelPanelScope(projectId, sessionId, detail)}
          {#if visited.includes('plan')}<div hidden={panel !== 'plan'}>
              <PlanPanel {projectId} {sessionId} closed={detail.session.closed_at !== null} />
            </div>{/if}
          {#if visited.includes('activity')}
            <div hidden={panel !== 'activity'}>
              <h2>Session activity</h2>
              <details>
                <summary>Session details</summary>
                <dl class="facts">
                  <dt>Environment</dt>
                  <dd>{detail.session.environment_id}</dd>
                  <dt>Opened</dt>
                  <dd>{formatTime(detail.session.opened_at)}</dd>
                  <dt>State</dt>
                  <dd>{detail.session.closed_at ? 'Closed' : 'Open'}</dd>
                </dl>
                <a href={projectHref(projectId)}>Project settings</a>
              </details>
              <FoldProposals {projectId} {sessionId} />
              {#if kernelScope}<KernelPanel
                  {projectId}
                  {sessionId}
                  environmentId={kernelScope.environmentId}
                />{/if}
              <JobsPanel {projectId} {sessionId} />
              {#await reviewComponent then component}{@const Reviews = component.default}<Reviews
                  {projectId}
                  {sessionId}
                  {reviewId}
                />{/await}
              <h2>Run history</h2>
              {#each detail.runs as run (run.name)}<RunSection
                  {projectId}
                  {sessionId}
                  {run}
                  initiallyOpen={false}
                />{:else}<p class="muted">No completed runs yet.</p>{/each}
            </div>
          {/if}
        {/if}
      </div>
    </aside>
  </div>
</div>
