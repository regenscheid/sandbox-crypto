<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { browserState } from '../lib/browserState';
  import AgentActivity from './AgentActivity.svelte';
  import { focusPanel } from '../lib/focusPanel';
  import { untrack } from 'svelte';
  import { api, type SessionDetail } from '../lib/api';
  import type { RunTree, StatusPage } from '../lib/api-types';
  import { chatApi } from '../lib/chat';
  import { get } from '../lib/http';
  import {
    activeSubagents,
    sessionStatus,
    subagentText,
    type ChatSignals,
  } from '../lib/sessionStatus';
  import { idle, submit } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  import SessionReviews from './SessionReviews.svelte';
  import SessionUsage from './SessionUsage.svelte';
  import SessionCompute from './SessionCompute.svelte';
  import { formatTime } from '../lib/format';
  import SessionDocuments from './SessionDocuments.svelte';
  import SessionAgents from './SessionAgents.svelte';
  import SessionContext from './SessionContext.svelte';
  import PaneResizer from './PaneResizer.svelte';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { projectHref } from '../lib/router';
  import { kernelPanelScope } from '../lib/kernels';
  import { rememberSession } from '../lib/lastSession';
  import ChatPanel from './ChatPanel.svelte';
  import ArtifactPanel from './ArtifactPanel.svelte';
  import type { ArtifactEntry } from '../lib/api-types';
  import { keptChoice, type ArtifactChoice } from '../lib/artifactStrip';
  import FoldProposals from './FoldProposals.svelte';
  import PlanPanel from './PlanPanel.svelte';
  import PollStatus from './PollStatus.svelte';
  import RunSection from './RunSection.svelte';

  let {
    projectId,
    sessionId,
    reviewId = null,
    agentId = null,
    openPanel = null,
  }: {
    projectId: string;
    sessionId: string;
    /** A review to choose in the review panel, as a link names it. */
    reviewId?: string | null;
    /** A delegation whose card to open in the Agents panel, as a link names it. */
    agentId?: string | null;
    /** A tab to open, as a link names it (the Usage tab's). */
    openPanel?: 'usage' | null;
  } = $props();
  type Panel =
    | 'plan'
    | 'agents'
    | 'context'
    | 'reviews'
    | 'compute'
    | 'usage'
    | 'documents'
    | 'artifacts'
    | 'activity';
  let panel = $state<Panel | null>(
    untrack(() =>
      agentId
        ? 'agents'
        : reviewId
          ? 'reviews'
          : openPanel
            ? openPanel
            : window.innerWidth > 1120
              ? 'plan'
              : null,
    ),
  );
  let visited = $state<Panel[]>(
    untrack(() =>
      agentId ? ['agents'] : reviewId ? ['reviews'] : openPanel ? [openPanel] : ['plan'],
    ),
  );
  /** The delegation whose card the Agents panel opens: a link's, or a chat block's. */
  let focusAgent = $state<string | null>(untrack(() => agentId));
  const browserScope = untrack(() => `session-browser:${projectId}:${sessionId}`);
  const keptBrowser = untrack(() => browserState.load(browserScope).draft ?? {});
  let activityCategory = $state(keptBrowser.category ?? 'all');
  let activityRun = $state(keptBrowser.run ?? '');
  /** The artifact the Artifacts panel shows, chosen from a strip or its list (B10, item 6). */
  let artifactChoice = $state<ArtifactChoice | null>(keptChoice(keptBrowser));
  $effect(() => {
    browserState.save(browserScope, {
      category: activityCategory,
      run: activityRun,
      ...(artifactChoice
        ? { artifact: artifactChoice.artifact, revision: String(artifactChoice.revision) }
        : {}),
    });
  });
  function chooseArtifact(entry: ArtifactEntry | null) {
    artifactChoice = entry
      ? { artifact: entry.artifact_id, revision: entry.revision, entry }
      : null;
  }
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
  // The run tree and the recovery status, for the header and the Agents and Reviews tabs.
  let tree = $state<RunTree | null>(null);
  let recovery = $state<StatusPage | null>(null);
  let signals = $state<ChatSignals | null>(null);
  const acting = $state(idle());
  $effect(() => {
    const project = projectId,
      session = sessionId;
    const at = `/projects/${encodeURIComponent(project)}/sessions/${encodeURIComponent(session)}`;
    return new Poller(
      (signal) =>
        Promise.all([
          api.runTree(project, session, signal),
          get<StatusPage>(`${at}/recovery`, { signal }),
        ]),
      (value) => {
        if (value.data) [tree, recovery] = value.data;
      },
      { intervalMs: 3000 },
    ).watch();
  });
  const subagents = $derived(activeSubagents(tree));
  const checks = $derived(
    tree?.delegations.filter((item) => item.kind === 'check' && item.ended === null).length ?? 0,
  );
  const status = $derived(
    sessionStatus({
      closed: view.data !== null && view.data.session.closed_at !== null,
      service: signals?.service ?? true,
      running: (signals?.lease ?? null) !== null,
      questions: signals?.questions ?? 0,
      held: signals?.held ?? false,
      recovery,
      subagents,
    }),
  );
  const tabs = $derived<{ id: Panel; label: string; badge: number }[]>([
    { id: 'plan', label: 'Plan', badge: 0 },
    {
      id: 'agents',
      label: 'Agents',
      badge: tree?.runs.filter((run) => run.state === 'running').length ?? 0,
    },
    { id: 'context', label: 'Context', badge: 0 },
    { id: 'reviews', label: 'Reviews', badge: checks },
    { id: 'compute', label: 'Compute', badge: 0 },
    { id: 'usage', label: 'Usage', badge: 0 },
    { id: 'documents', label: 'Documents', badge: 0 },
    { id: 'artifacts', label: 'Artifacts', badge: 0 },
    { id: 'activity', label: 'Activity log', badge: 0 },
  ]);
  function choose(id: Panel) {
    if (!visited.includes(id)) visited = [...visited, id];
    panel = panel === id ? null : id;
  }
  function show(id: Panel) {
    if (!visited.includes(id)) visited = [...visited, id];
    panel = id;
  }
  async function stop() {
    const run = signals?.lease;
    if (run)
      await submit(acting, () => chatApi.cancel(projectId, sessionId, run).then(() => undefined));
  }
  async function resume() {
    await submit(acting, () => chatApi.resume(projectId, sessionId).then(() => undefined));
  }
</script>

<svelte:window
  onresize={() => {
    viewportWidth = window.innerWidth;
    width = Math.min(width, maxWidth);
  }}
/>
<div class="session-workspace" style:--inspector-width={`${width}px`}>
  <header class="session-header" inert={narrow && panel !== null}>
    <div class="session-title">
      <span class="eyebrow">Session</span>
      <h1>{sessionId}</h1>
    </div>
    <button
      type="button"
      class="status-pill tone-{status.tone}"
      title="Show the session's agents"
      onclick={() => show('agents')}
      ><span class="status-dot" aria-hidden="true"></span><strong>{status.label}</strong
      >{#if status.detail}<span>· {status.detail}</span>{/if}</button
    >
    {#if signals?.lease && status.tone !== 'closed'}<button
        type="button"
        disabled={acting.busy}
        title="Stop the run that holds the session; its background jobs keep running"
        onclick={stop}>Stop</button
      >{:else if status.tone === 'stopped'}<button
        type="button"
        class="primary-button"
        disabled={acting.busy}
        onclick={resume}>Resume</button
      >{/if}
    <div class="research-actions" aria-label="Research views">
      {#each tabs as tab (tab.id)}<button
          data-panel={tab.id}
          class:active={panel === tab.id}
          aria-pressed={panel === tab.id}
          aria-controls="research-pane"
          onclick={() => choose(tab.id)}
          >{tab.label}{#if tab.badge}<span class="tab-badge">{tab.badge}</span>{/if}</button
        >{/each}
    </div>
    {#if acting.error}<ErrorMessage error={acting.error} />{/if}
  </header>
  <div class="session-columns">
    <div class="conversation-column" inert={narrow && panel !== null}>
      <PollStatus phase={view.phase} hasData={view.data !== null} what="Session views" />
      {#if view.data}
        <ChatPanel
          {projectId}
          {sessionId}
          closed={view.data.session.closed_at !== null}
          bind:signals
          subagents={subagentText(subagents)}
          showAgents={() => show('agents')}
          showArtifact={(entry) => {
            chooseArtifact(entry);
            show('artifacts');
          }}
          {tree}
          showAgent={(delegation) => {
            focusAgent = delegation;
            show('agents');
          }}
        />
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
        {#if visited.includes('artifacts')}<div hidden={panel !== 'artifacts'}>
            <ArtifactPanel
              {projectId}
              {sessionId}
              choice={artifactChoice}
              choose={chooseArtifact}
            />
          </div>{/if}
        {#if visited.includes('agents')}<div hidden={panel !== 'agents'}>
            <SessionAgents {projectId} {sessionId} {tree} focus={focusAgent} />
          </div>{/if}
        {#if visited.includes('context')}<div hidden={panel !== 'context'}>
            <SessionContext {projectId} {sessionId} {tree} />
          </div>{/if}
        {#if visited.includes('reviews')}<div hidden={panel !== 'reviews'}>
            <SessionReviews {projectId} {sessionId} {reviewId} {tree} />
          </div>{/if}
        {#if visited.includes('usage')}<div hidden={panel !== 'usage'}>
            <SessionUsage {projectId} {sessionId} active={panel === 'usage'} />
          </div>{/if}
        {#if view.data}
          {@const detail = view.data}
          {@const kernelScope = kernelPanelScope(projectId, sessionId, detail)}
          {#if visited.includes('compute')}<div hidden={panel !== 'compute'}>
              <SessionCompute {projectId} {sessionId} {tree} {kernelScope} />
            </div>{/if}
          {#if visited.includes('plan')}<div hidden={panel !== 'plan'}>
              <PlanPanel {projectId} {sessionId} closed={detail.session.closed_at !== null} />
            </div>{/if}
          {#if visited.includes('activity')}
            <div hidden={panel !== 'activity'}>
              <h2>Activity log</h2>
              <p class="muted">Every event of every run, for when you need the detail.</p>
              <label
                >Activity source <select bind:value={activityCategory}
                  ><option value="all">All sources</option><option value="runs">Run events</option
                  ><option value="folds">Fold proposals</option></select
                ></label
              >
              <label
                >Selected run <select bind:value={activityRun}
                  ><option value="">Choose a run</option
                  >{#each detail.runs as run (run.name)}<option value={run.name}>{run.name}</option
                    >{/each}</select
                ></label
              >
              {#if activityRun}<div hidden={!['all', 'runs'].includes(activityCategory)}>
                  {#key activityRun}<AgentActivity
                      {projectId}
                      {sessionId}
                      run={activityRun}
                    />{/key}
                </div>{/if}
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
              <div hidden={!['all', 'folds'].includes(activityCategory)}>
                <FoldProposals {projectId} {sessionId} />
              </div>
              <div hidden={!['all', 'runs'].includes(activityCategory)}>
                <h2>Run history</h2>
                {#each detail.runs as run (run.name)}<RunSection
                    {projectId}
                    {sessionId}
                    {run}
                    initiallyOpen={false}
                  />{:else}<p class="muted">No completed runs yet.</p>{/each}
              </div>
            </div>
          {/if}
        {/if}
      </div>
    </aside>
  </div>
</div>
