<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { browserState } from '../lib/browserState';
  import { formatTime } from '../lib/format';
  import {
    readSessionPlan,
    currentStepStatus,
    planCounts,
    authorLabel,
    type PlanObservation,
  } from '../lib/plans';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { visible } from '../lib/text';
  import PlanEditor from './PlanEditor.svelte';
  let {
    projectId,
    sessionId,
    closed = false,
    initialState = initialPoll(),
  }: {
    projectId: string;
    sessionId: string;
    closed?: boolean;
    initialState?: PollState<PlanObservation>;
  } = $props();
  let viewState = $state<PollState<PlanObservation>>(untrack(() => initialState));
  const scope = untrack(() => `plan-browser:${projectId}:${sessionId}`);
  const kept = untrack(() => browserState.load(scope).draft ?? {});
  let search = $state(kept.search ?? '');
  let statusFilter = $state(kept.status ?? 'all');
  let selectedStep = $state(kept.step ?? '');
  $effect(() => {
    browserState.save(scope, { search, status: statusFilter, step: selectedStep });
  });
  let mounted = $state(false);
  let refreshTick = $state(0);
  const data = $derived(
    viewState.data?.project_id === projectId && viewState.data.session_id === sessionId
      ? viewState.data
      : null,
  );
  const plan = $derived(data?.plan);
  const counts = $derived(plan ? planCounts(plan) : null);
  onMount(() => {
    mounted = true;
  });
  $effect(() => {
    if (!mounted) return;
    refreshTick;
    const scope = { projectId, sessionId };
    const previous = untrack(() => viewState.data);
    viewState = {
      phase: 'loading',
      data:
        previous?.project_id === projectId && previous.session_id === sessionId ? previous : null,
      busy: false,
    };
    // A closed session's plan is read once; an open one until it is seen closed.
    const poller = new Poller(
      (signal) => readSessionPlan(scope, signal),
      (value) => {
        viewState = value;
      },
      { once: closed, done: (found) => found.session_closed_at !== null },
    );
    return poller.watch();
  });
</script>

<section aria-label="Session plan" class="plan-panel">
  <h2 class="sr-only">Session plan</h2>
  {#if viewState.phase === 'loading'}<p role="status">Loading the recorded plan…</p>
  {:else if viewState.phase === 'paused'}<p role="status">
      Plan updates paused while this page is hidden.
    </p>
  {:else if viewState.phase === 'signed_out'}<p role="status">
      Plan updates stopped for sign-out. Reload after signing in.
    </p>
  {:else if viewState.phase === 'missing'}<p role="status">
      This session is unavailable. Plan updates stopped.
    </p>
  {:else if viewState.phase === 'unavailable'}<p role="status">
      Plan updates unavailable. The current plan state is unknown.
    </p>{/if}
  {#if data}
    {#if viewState.phase !== 'observed'}<p>
        <strong>Previous observation; current plan state is unknown.</strong>
      </p>{/if}
    {#if data.session_closed_at !== null}<p class="muted">
        Session closed; showing its recorded plan.
      </p>{/if}
    {#if plan && counts}
      <div class="plan-heading">
        <span class="plan-status status-{plan.head.status}"
          >{plan.head.status.replaceAll('_', ' ')}</span
        >
        <span class="muted"
          >Version {plan.head.ref.version} · {authorLabel(plan.version.author)} ·
          {formatTime(plan.version.recorded_at)}</span
        >
      </div>
      <p class="goal">{visible(plan.version.body.goal)}</p>
      <div class="progress">
        <p class="progress-summary">
          {counts.completed} of {plan.steps.length} steps completed · {counts.in_progress} in progress
          · {counts.blocked} blocked · {counts.skipped} skipped
        </p>
        <div
          class="bar"
          role="progressbar"
          aria-label="Completed plan steps"
          aria-valuemin="0"
          aria-valuemax={plan.steps.length}
          aria-valuenow={counts.completed}
        >
          <span style:width={`${(100 * counts.completed) / Math.max(plan.steps.length, 1)}%`}
          ></span>
        </div>
      </div>
      {#if plan.steps.length > 10}
        <div class="filters">
          <label class="sr-only" for="plan-search">Search plan steps</label>
          <input id="plan-search" type="search" placeholder="Search steps" bind:value={search} />
          <label class="sr-only" for="plan-progress">Progress</label>
          <select id="plan-progress" bind:value={statusFilter}
            ><option value="all">All steps</option
            >{#each ['pending', 'in_progress', 'completed', 'blocked', 'skipped'] as status}<option
                value={status}>{status.replaceAll('_', ' ')}</option
              >{/each}</select
          >
        </div>
      {/if}
      {#each plan.version.body.phases as phase (phase.phase_id)}
        <h3 class="phase">{visible(phase.title)}</h3>
        <ol class="steps">
          {#each phase.steps as step (step.step_id)}
            {@const view = plan.steps.find((value) => value.step_id === step.step_id)!}
            {@const status = currentStepStatus(view)}
            <li
              hidden={!(statusFilter === 'all' || status === statusFilter) ||
                !`${visible(step.title)} ${visible(step.description)} ${step.step_id}`
                  .toLocaleLowerCase()
                  .includes(search.toLocaleLowerCase())}
            >
              <button
                type="button"
                class="step-row"
                aria-expanded={selectedStep === step.step_id}
                onclick={() => (selectedStep = selectedStep === step.step_id ? '' : step.step_id)}
              >
                <span class="step-mark mark-{status}" aria-hidden="true"></span>
                <span class="step-title">{visible(step.title)}</span>
                <span class="step-status status-{status}">{status.replaceAll('_', ' ')}</span>
              </button>
              <div class="step-detail" hidden={selectedStep !== step.step_id}>
                <p class="prose">{visible(step.description)}</p>
                <p class="muted small">
                  {#if step.track_id}Track {visible(
                      plan.version.body.tracks.find((track) => track.track_id === step.track_id)
                        ?.title ?? step.track_id,
                    )} ·
                  {/if}<code>{step.step_id}</code>
                </p>
                <details class="quiet">
                  <summary>Evidence standard</summary>
                  <p class="prose">{visible(step.evidence_standard)}</p>
                </details>
                {#if view.latest}
                  {#if !view.matches_current_step}<p class="previous-step">
                      Previous step version was {view.latest.status.replaceAll('_', ' ')}; this
                      revised step needs a new progress update.
                    </p>{/if}
                  <p class="muted small">
                    {view.matches_current_step ? 'Updated' : 'Previous update'} by {authorLabel(
                      view.latest.author,
                    )} · {formatTime(view.latest.recorded_at)} · {view.latest.evidence_count} recorded
                    evidence references
                  </p>
                {/if}
              </div>
            </li>
          {/each}
        </ol>
      {/each}
      {#if plan.head.outcome}
        <div class="outcome">
          <h3>Reported goal outcome: {plan.head.outcome.status.replaceAll('_', ' ')}</h3>
          <p class="prose">{visible(plan.head.outcome.summary)}</p>
          {#if plan.head.outcome.remaining_work.length}<p>Remaining work:</p>
            <ul>
              {#each plan.head.outcome.remaining_work as item}<li class="prose">{item}</li>{/each}
            </ul>{/if}
          <p class="muted">
            {plan.head.outcome.evidence.length} recorded outcome evidence references.
          </p>
        </div>
      {/if}
      <details class="quiet about">
        <summary>About this version</summary>
        <p class="muted prose">Reason for this version: {visible(plan.version.reason)}</p>
        <p class="muted prose">{visible(plan.version.body.feasibility)}</p>
        {#if plan.version.body.outputs.length}
          <p class="muted">Expected outputs:</p>
          <ul>
            {#each plan.version.body.outputs as output}<li>
                <strong>{visible(output.title)}</strong>
                <p class="prose">{visible(output.description)}</p>
                <p class="muted prose">Evidence: {visible(output.evidence_standard)}</p>
              </li>{/each}
          </ul>
        {/if}
        <p class="muted small">
          Plan <code>{plan.head.ref.plan_id}</code> · read at
          <time datetime={data.observed_at}>{formatTime(data.observed_at)}</time>. Progress records
          attributed updates; “in progress” does not prove a job is running. This compact view omits
          progress notes and reference details.
        </p>
      </details>
      {#key `${projectId}:${sessionId}:${plan.head.ref.plan_id}`}
        <PlanEditor
          {projectId}
          {sessionId}
          observation={data}
          observed={viewState.phase === 'observed' && !closed}
          refresh={() => {
            refreshTick += 1;
          }}
        />
      {/key}
    {:else}
      <p>
        {viewState.phase === 'observed'
          ? 'No plan has been recorded for this session.'
          : 'No plan was present in the previous observation.'}
      </p>
    {/if}
  {/if}
</section>

<style>
  .plan-panel {
    margin-block: 0.5rem 1.5rem;
    color: var(--text);
  }
  .plan-heading {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px 10px;
    font-size: 12px;
  }
  .plan-status,
  .step-status {
    display: inline-block;
    font-size: 12px;
    font-weight: 500;
  }
  .plan-status::first-letter,
  .step-status::first-letter {
    text-transform: uppercase;
  }
  .plan-status {
    padding: 1px 8px;
    border-radius: 9px;
    background: var(--subtle);
    color: var(--body-text);
  }
  .plan-status.status-active {
    background: var(--ok-soft);
    color: var(--ok);
  }
  .goal {
    margin: 10px 0 14px;
    font-size: 15px;
    font-weight: 600;
    line-height: 1.45;
    white-space: pre-wrap;
  }
  .progress-summary {
    margin: 0 0 6px;
    font-size: 13px;
    color: var(--body-text);
  }
  .bar {
    height: 6px;
    border-radius: 3px;
    background: var(--subtle);
    overflow: hidden;
    margin-bottom: 18px;
  }
  .bar span {
    display: block;
    height: 100%;
    border-radius: 3px;
    background: var(--ok-dot);
  }
  .filters {
    display: flex;
    gap: 8px;
    margin-bottom: 12px;
  }
  .filters input {
    flex: 1 1 auto;
    min-width: 0;
  }
  .phase {
    margin: 18px 0 4px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .steps {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .step-row {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    width: 100%;
    min-height: 0;
    padding: 7px 6px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: var(--text);
    font: inherit;
    text-align: left;
  }
  .step-row:hover {
    background: var(--subtle);
  }
  .step-title {
    flex: 1 1 auto;
    min-width: 0;
  }
  .step-status {
    flex: none;
    color: var(--muted);
  }
  .step-status.status-in_progress {
    color: var(--link);
  }
  .step-status.status-blocked {
    color: var(--warn-text);
  }
  /* A ring for each step's state: filled when done, half when under way. */
  .step-mark {
    flex: none;
    width: 14px;
    height: 14px;
    margin-top: 3px;
    border-radius: 50%;
    border: 1.5px solid var(--border-strong);
  }
  .mark-completed {
    border-color: var(--ok-dot);
    background: var(--ok-dot);
    box-shadow: inset 0 0 0 2.5px var(--background);
  }
  .mark-in_progress {
    border-color: var(--link);
    background: linear-gradient(90deg, var(--link) 50%, transparent 50%);
  }
  .mark-blocked {
    border-color: var(--warn-dot);
    background: var(--warn-soft);
  }
  .mark-skipped {
    border-style: dashed;
  }
  .step-detail {
    margin: 0 6px 10px 30px;
    font-size: 13px;
  }
  .step-detail p {
    margin: 4px 0;
  }
  .small {
    font-size: 12px;
  }
  .quiet summary {
    color: var(--muted);
    font-size: 12px;
    cursor: pointer;
  }
  .about {
    margin-top: 20px;
  }
  .prose {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .previous-step {
    font-size: 0.9rem;
  }
  .outcome {
    margin-block: 1rem;
    padding: 0.75rem;
    border-left: 3px solid var(--accent-line);
  }
</style>
