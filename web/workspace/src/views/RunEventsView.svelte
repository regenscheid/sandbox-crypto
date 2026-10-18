<!-- SPDX-License-Identifier: MIT -->
<!-- One run's events, as it writes them (docs/specs/run-service.md, RS-14, RS-15; PD-42):
     a main or delegated run, running or ended, its transcript as the chat panel shows a
     run's, read a page at a time from the start, and its newer events on request while it
     runs. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { chatApi, PAGE, type WatchedRun } from '../lib/chat';
  import { projectHref, sessionHref, treeHref } from '../lib/router';
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
<h1>Run {visible(name)}</h1>
<p class="muted">
  {shown.state === 'active'
    ? 'Running: its newer events are read on request.'
    : shown.state === 'ended'
      ? 'Ended.'
      : 'Interrupted: its process died.'}
</p>
<SavedContext project={projectId} session={sessionId} run={name} />
<ChatRun run={shown} />
{#if !loading && !error && shown.events.length === 0}<p class="muted">No events yet.</p>{/if}
{#if loading}<p class="muted">Loading…</p>{/if}
{#if error}<ErrorMessage {error} />{/if}
{#if !loading && (shown.state === 'active' || !caught)}
  <button type="button" onclick={read}>{caught ? 'Read newer events' : 'Read more events'}</button>
{/if}
