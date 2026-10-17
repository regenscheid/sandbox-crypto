<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { chatApi, type WatchedRun } from '../lib/chat';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { runHref } from '../lib/router';
  import ChatRun from './ChatRun.svelte';
  import PollStatus from './PollStatus.svelte';
  let { projectId, sessionId, run }: { projectId: string; sessionId: string; run: string } =
    $props();
  let view = $state<PollState<WatchedRun>>(initialPoll());
  let trimmed = $state(false);
  $effect(() => {
    const project = projectId,
      session = sessionId,
      name = run;
    let events: WatchedRun['events'] = [];
    let after = 0;
    const poller = new Poller(
      async (signal) => {
        const page = await chatApi.events(project, session, name, after, signal);
        events = [...events, ...page.events];
        after = events.at(-1)?.seq ?? after;
        if (events.length > 400) {
          events = events.slice(-400);
          trimmed = true;
        }
        return { run: name, state: page.state, events };
      },
      (value) => {
        view = value;
      },
      { intervalMs: 1500 },
    );
    return poller.watch();
  });
</script>

<div class="agent-activity">
  <h3>Activity</h3>
  <PollStatus phase={view.phase} hasData={view.data !== null} what="Agent events" />
  {#if trimmed}<p class="muted">
      Showing the latest 400 loaded events. <a href={runHref(projectId, sessionId, run)}
        >Full history</a
      >
    </p>{/if}
  {#if view.data}<ChatRun run={view.data} {projectId} />{/if}
</div>
