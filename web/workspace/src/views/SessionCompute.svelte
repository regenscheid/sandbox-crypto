<!-- SPDX-License-Identifier: MIT -->
<!-- What computes for the session now, in one place for the main agent and every subagent:
     the tool calls each running agent has made and has no result for yet (a shell or
     Python call still executing), the session's background jobs, its kernels, and each
     child environment a subagent works in, with that environment's kernels (CE-8). The
     calls are read from each running agent's events; their text is agents' text, shown
     with what a person may not see as its code point. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import type { KernelInfo, KernelObservation, KernelScope } from '../lib/kernels';
  import type { RunEvent, RunTree, TreeRun } from '../lib/api-types';
  import { chatApi, chatEntries, PAGE } from '../lib/chat';
  import {
    callSummary,
    clock,
    elapsed,
    foldTranscript,
    rowLabel,
    type StepCall,
  } from '../lib/conversation';
  import { kernelCells, kernelNotObserved, type PendingCall } from '../lib/kernelCells';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { agentName } from '../lib/runTree';
  import { visible } from '../lib/text';
  import ChildEnvironments from './ChildEnvironments.svelte';
  import JobsPanel from './JobsPanel.svelte';
  import KernelPanel from './KernelPanel.svelte';
  import PollStatus from './PollStatus.svelte';

  let {
    projectId,
    sessionId,
    tree,
    kernelScope = null,
  }: {
    projectId: string;
    sessionId: string;
    tree: RunTree | null;
    kernelScope?: KernelScope | null;
  } = $props();

  interface Running {
    run: TreeRun;
    calls: { call: StepCall; at: string }[];
  }
  let view = $state<PollState<Running[]>>(initialPoll());
  let now = $state(Date.now());
  /** The kernel panel's latest observation, which each kernel's current cell is joined to. */
  let observation = $state<PollState<KernelObservation> | null>(null);
  /** Each child environment's latest kernel observation, by its ID (CE-8). */
  let childObservations = $state<Record<string, PollState<KernelObservation>>>({});

  /** The calls of a run's events that have no result yet, with when each was made. */
  function pending(events: readonly RunEvent[]): Running['calls'] {
    return foldTranscript(chatEntries(events), events).flatMap((item) =>
      item.kind === 'work'
        ? item.steps.flatMap((step) =>
            step.calls.filter((call) => !call.done).map((call) => ({ call, at: step.at })),
          )
        : [],
    );
  }

  $effect(() => {
    const project = projectId,
      session = sessionId;
    // Each running agent's events read so far, read on from the last one at each poll.
    const read = new Map<string, RunEvent[]>();
    return new Poller(
      async (signal) => {
        const live = untrack(() => tree?.runs ?? []).filter((run) => run.state === 'running');
        for (const name of read.keys())
          if (!live.some((run) => run.name === name)) read.delete(name);
        const found: Running[] = [];
        for (const run of live) {
          let events = read.get(run.name) ?? [];
          for (let page = 0; page < 5; page++) {
            const listed = await chatApi.events(
              project,
              session,
              run.name,
              events.at(-1)?.seq ?? 0,
              signal,
            );
            events = [...events, ...listed.events];
            if (listed.events.length < PAGE) break;
          }
          read.set(run.name, events);
          found.push({ run, calls: pending(events) });
        }
        now = Date.now();
        return found;
      },
      (value) => {
        view = value;
      },
      { intervalMs: 3000 },
    ).watch();
  });
  /** A call that waits on other work, rather than computing. */
  const callState = (call: StepCall) => (call.name.startsWith('wait') ? 'waiting' : 'running');
  const delegation = (run: TreeRun) =>
    tree?.delegations.find((item) => item.delegation_id === run.delegation) ?? null;
  const calls = $derived<PendingCall[]>(
    (view.data ?? []).flatMap(({ run, calls }) => calls.map((item) => ({ ...item, run }))),
  );
  /** The session's kernels and its child environments', as far as each was observed. */
  const kernels = $derived.by(() => {
    const children = Object.values(childObservations).flatMap((state) => state.data?.kernels ?? []);
    const own = observation?.data?.kernels ?? null;
    return own === null && children.length === 0 ? null : [...(own ?? []), ...children];
  });
  /** Each observed kernel's current cell: its owner's oldest cell call without a result. */
  const cells = $derived(kernelCells(kernels ?? [], calls));
</script>

{#snippet detail(kernel: KernelInfo)}
  {@const current = cells.get(kernel.kernel_id) ?? null}
  {@const label = current ? rowLabel(current.call) : null}
  <p class="kernel-cell" data-kernel={kernel.kernel_id}>
    {#if current && label}
      <strong>Running now</strong> ({agentName(current.run, delegation(current.run))}):
      {#if label.says}
        <span class="muted">the agent's words:</span> {label.text}
      {:else}
        <span class="muted">its code, first line:</span> <code>{label.text}</code>
      {/if}
      · since {clock(current.at)} · {elapsed(current.at, now)}
    {:else}
      <strong>Idle</strong>
      <span class="muted">(no {kernel.language} call is waiting on this kernel)</span>
    {/if}
  </p>
{/snippet}

<section aria-label="Compute">
  <h2>Running now</h2>
  <p class="muted lead">Tool calls still executing, for the research agent and every subagent.</p>
  <PollStatus phase={view.phase} hasData={view.data !== null} what="Running calls" />
  {#each calls as { call, at, run }, index (index)}
    <article class="agent-card">
      <div class="agent-heading">
        <span class="session-dot" aria-hidden="true"></span>
        <code>{visible(call.name)}</code>
        <span class="pill {callState(call)}">{callState(call)} {elapsed(at, now)}</span>
        <span class="muted">since {clock(at)}</span>
      </div>
      {#if callSummary(call)}<p class="agent-task mono">{callSummary(call)}</p>{/if}
      <p class="agent-detail">{agentName(run, delegation(run))}</p>
      {#if kernelNotObserved({ call, at, run }, kernels)}
        <p class="muted">
          Its kernel was not observed (the observation is missing, stale or lacks it), so it is not
          listed under the session's or a child environment's kernels.
        </p>
      {/if}
    </article>
  {:else}
    {#if view.data}<p class="empty-note">Nothing is executing now.</p>{/if}
  {/each}
  <JobsPanel {projectId} {sessionId} />
  {#if kernelScope}<KernelPanel
      {projectId}
      {sessionId}
      environmentId={kernelScope.environmentId}
      onobserve={(state) => {
        observation = state;
      }}
      {detail}
    />{/if}
  {#if kernelScope}<ChildEnvironments
      {projectId}
      {sessionId}
      kernels
      heading="Subagents' own environments"
      onobserve={(environment, state) => {
        childObservations = { ...childObservations, [environment]: state };
      }}
      {detail}
    />{/if}
</section>

<style>
  .lead {
    margin: 0 0 12px;
  }
  .mono {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
</style>
