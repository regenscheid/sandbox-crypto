<!-- SPDX-License-Identifier: MIT -->
<!-- What computes for the session now, in one place for the main agent and every subagent:
     the tool calls each running agent has made and has no result for yet (a shell or
     Python call still executing), the session's background jobs, and its kernels. The
     calls are read from each running agent's events; their text is agents' text, shown
     with what a person may not see as its code point. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import type { KernelScope } from '../lib/kernels';
  import type { RunEvent, RunTree, TreeRun } from '../lib/api-types';
  import { chatApi, chatEntries, PAGE } from '../lib/chat';
  import { callSummary, clock, elapsed, foldTranscript, type StepCall } from '../lib/conversation';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { agentName } from '../lib/runTree';
  import { visible } from '../lib/text';
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
  const calls = $derived(
    (view.data ?? []).flatMap(({ run, calls }) => calls.map((item) => ({ ...item, run }))),
  );
</script>

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
    </article>
  {:else}
    {#if view.data}<p class="empty-note">Nothing is executing now.</p>{/if}
  {/each}
  <JobsPanel {projectId} {sessionId} />
  {#if kernelScope}<KernelPanel
      {projectId}
      {sessionId}
      environmentId={kernelScope.environmentId}
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
