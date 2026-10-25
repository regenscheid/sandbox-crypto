<!-- SPDX-License-Identifier: MIT -->
<!-- One run's events, as it writes them (docs/specs/run-service.md, RS-14, RS-15; PD-42):
     a main or delegated run, running or ended, its transcript as the chat panel shows a
     run's, read a page at a time from the start, and its newer events on request while it
     runs. Above it, a breadcrumb from the session through the agents that delegated it,
     by name, each leading to its own transcript (B10, item 3), and for a delegated run its
     brief's title, its state and a link to its delegation card. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { RunTree } from '../lib/api-types';
  import { chatApi, PAGE, type WatchedRun } from '../lib/chat';
  import { agentHref, projectHref, runHref, sessionHref, treeHref } from '../lib/router';
  import { agentName, briefView, endedText, lineage, stateText } from '../lib/runTree';
  import { visible } from '../lib/text';
  import ChatRun from './ChatRun.svelte';
  import SavedContext from './SavedContext.svelte';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId, sessionId, run }: { projectId: string; sessionId: string; run: string } =
    $props();
  // The route keys this view by the project, session and run: they never change here.
  const name = untrack(() => run);
  const shown = $state<WatchedRun>({ run: name, state: 'active', events: [] });
  let loading = $state(false);
  let error = $state<unknown>(null);
  /** Whether the read reached the run's last event written so far. */
  let caught = $state(false);
  /** The session's run tree, for the agents' names; null until read, or if it fails. */
  let tree = $state<RunTree | null>(null);
  const delegationOf = (delegation: string | null) =>
    delegation === null
      ? null
      : (tree?.delegations.find((item) => item.delegation_id === delegation) ?? null);
  /** The runs from the session's main agent down to this one, each with its name. */
  const trail = $derived(
    lineage(tree?.runs ?? [], name).map((run) => ({
      run,
      name: agentName(run, delegationOf(run.delegation)),
    })),
  );
  const self = $derived(trail.at(-1)?.run.name === name ? trail.at(-1)! : null);
  const record = $derived(delegationOf(self?.run.delegation ?? null));

  /** The tree, for the breadcrumb; a failed read leaves the run's name alone. */
  async function readTree(): Promise<void> {
    try {
      tree = await api.runTree(projectId, sessionId);
    } catch {
      // The transcript stands without the names.
    }
  }
  void readTree();

  /** Read the events after those shown, a page at a time, as far as ten pages. */
  async function read(): Promise<void> {
    if (loading) return;
    loading = true;
    error = null;
    try {
      for (let page = 0; page < 10; page++) {
        const after = shown.events.at(-1)?.seq ?? 0;
        const found = await chatApi.events(projectId, sessionId, name, after);
        shown.state = found.state;
        shown.events = [...shown.events, ...found.events];
        caught = found.events.length < PAGE;
        if (caught) break;
      }
    } catch (caught) {
      error = caught;
    } finally {
      loading = false;
    }
  }
  void read();
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
  <a href={sessionHref(projectId, sessionId)}>{sessionId}</a> /
  <a href={treeHref(projectId, sessionId)}>Run tree</a> /
</nav>
<nav class="agent-trail" aria-label="Breadcrumb">
  <a href={sessionHref(projectId, sessionId)}>Session</a
  >{#each trail as step (step.run.name)}{' › '}{#if step.run.name === name}<span aria-current="page"
        >{step.name}</span
      >{:else}<a href={runHref(projectId, sessionId, step.run.name)}>{step.name}</a
      >{/if}{/each}{#if !self}{' › '}<span aria-current="page">{visible(name)}</span>{/if}
</nav>
<h1>{self ? self.name : `Run ${visible(name)}`}</h1>
{#if self && self.run.role !== 'main'}
  <p class="muted">Run {visible(name)}</p>
  {#if record}
    {@const brief = briefView(record)}
    {#if brief.task}<p class="said">
        {brief.task.split('\n').find((line) => line.trim()) ?? ''}
      </p>{/if}
    <p>
      {self.run.state === 'running' ? 'Running.' : endedText(record)}
      <a href={agentHref(projectId, sessionId, record.delegation_id)}>Its delegation card</a>
    </p>
  {:else}<p>{stateText(self.run)}</p>{/if}
{/if}
<p class="muted">
  {shown.state === 'active'
    ? 'Running: its newer events are read on request.'
    : shown.state === 'ended'
      ? 'Ended.'
      : 'Interrupted: its process died.'}
</p>
<SavedContext project={projectId} session={sessionId} run={name} />
<ChatRun run={shown} {projectId} {sessionId} {tree} role={self?.run.role ?? null} />
{#if !loading && !error && shown.events.length === 0}<p class="muted">No events yet.</p>{/if}
{#if loading}<p class="muted">Loading…</p>{/if}
{#if error}<ErrorMessage {error} />{/if}
{#if !loading && (shown.state === 'active' || !caught)}
  <button
    type="button"
    onclick={() => {
      void read();
      void readTree();
    }}>{caught ? 'Read newer events' : 'Read more events'}</button
  >
{/if}
