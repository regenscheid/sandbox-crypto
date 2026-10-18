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
  <h2>Session plan</h2>
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
    <p class="muted">
      Read at <time datetime={data.observed_at}>{formatTime(data.observed_at)}</time>.
      {#if data.session_closed_at !== null}Session closed; showing its recorded plan.{/if}
    </p>
    {#if plan && counts}
      <div class="plan-heading">
        <span class="plan-status">{plan.head.status.replaceAll('_', ' ')}</span>
        <span class="muted"
          >Version {plan.head.ref.version} · <code>{plan.head.ref.plan_id}</code></span
        >
      </div>
      <p class="goal">{visible(plan.version.body.goal)}</p>
      <p class="muted">
        Plan by {authorLabel(plan.version.author)} · {formatTime(plan.version.recorded_at)}
      </p>
      <p class="muted prose">Reason for this version: {visible(plan.version.reason)}</p>
      <p class="progress-summary">
        {counts.completed} of {plan.steps.length} steps completed · {counts.in_progress} in progress ·
        {counts.blocked} blocked · {counts.skipped} skipped
      </p>
      <progress max={plan.steps.length} value={counts.completed} aria-label="Completed plan steps"
      ></progress>
      <details>
        <summary>Feasibility and expected outputs</summary>
        <p class="prose">{visible(plan.version.body.feasibility)}</p>
        <ul>
          {#each plan.version.body.outputs as output}<li>
              <strong>{visible(output.title)}</strong>
              <p class="prose">{visible(output.description)}</p>
              <p class="muted prose">Evidence: {visible(output.evidence_standard)}</p>
            </li>{/each}
        </ul>
      </details>
      <label>Search plan steps <input type="search" bind:value={search} /></label>
      <label
        >Progress <select bind:value={statusFilter}
          ><option value="all">All</option
          >{#each ['pending', 'in_progress', 'completed', 'blocked', 'skipped'] as status}<option
              value={status}>{status.replaceAll('_', ' ')}</option
            >{/each}</select
        ></label
      >
      {#each plan.version.body.phases as phase (phase.phase_id)}
        <h3>{visible(phase.title)}</h3>
        <ol class="steps">
          {#each phase.steps as step (step.step_id)}
            {@const view = plan.steps.find((value) => value.step_id === step.step_id)!}
            {@const status = currentStepStatus(view)}
            <li
              hidden={!(statusFilter === 'all' || status === statusFilter) ||
                !`${visible(step.title)} ${visible(step.description)} ${step.step_id}`
                  .toLocaleLowerCase()
                  .includes(search.toLocaleLowerCase())}
              class:step-completed={status === 'completed'}
              class:step-blocked={status === 'blocked'}
            >
              <div class="step-heading">
                <button
                  aria-expanded={selectedStep === step.step_id}
                  onclick={() => (selectedStep = selectedStep === step.step_id ? '' : step.step_id)}
                  >{visible(step.title)}</button
                ><span class="step-status">{status.replaceAll('_', ' ')}</span>
              </div>
              <div hidden={selectedStep !== step.step_id}>
                <p class="muted">
                  <code>{step.step_id}</code>{#if step.track_id}
                    · Track {visible(
                      plan.version.body.tracks.find((track) => track.track_id === step.track_id)
                        ?.title ?? step.track_id,
                    )}{/if}
                </p>
                <p class="prose">{visible(step.description)}</p>
                <details>
                  <summary>Evidence standard</summary>
                  <p class="prose">{visible(step.evidence_standard)}</p>
                </details>
                {#if view.latest}
                  {#if !view.matches_current_step}<p class="previous-step">
                      Previous step version was {view.latest.status.replaceAll('_', ' ')}; this
                      revised step needs a new progress update.
                    </p>{/if}
                  <p class="muted">
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
      <p class="muted">
        Progress records attributed updates; “in progress” does not prove a job is running. This
        compact view omits progress notes and reference details.
      </p>
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
    margin-block: 1.5rem;
  }
  .plan-heading,
  .step-heading {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.75rem;
  }
  .plan-status,
  .step-status {
    font-size: 0.85rem;
    padding: 0.15rem 0.5rem;
    border: 1px solid var(--border);
    border-radius: 0.25rem;
  }
  .goal {
    font-size: 1.1rem;
    font-weight: 600;
    white-space: pre-wrap;
  }
  progress {
    width: min(100%, 32rem);
    margin-bottom: 0.75rem;
  }
  .steps {
    padding-left: 1.5rem;
  }
  .steps > li {
    padding: 0.75rem;
    margin-bottom: 0.5rem;
    border: 1px solid var(--border);
    border-radius: 0.3rem;
  }
  .step-completed {
    border-left: 3px solid var(--ok);
  }
  .step-blocked {
    border-left: 3px solid var(--approximate);
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
    border: 1px solid var(--border);
    border-radius: 0.3rem;
  }
</style>
