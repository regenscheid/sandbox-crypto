<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { browserState } from '../lib/browserState';
  import { formatTime } from '../lib/format';
  import { visible } from '../lib/text';
  import { chatApi, type WatchedRun } from '../lib/chat';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { runHref } from '../lib/router';
  import ChatRun from './ChatRun.svelte';
  import PollStatus from './PollStatus.svelte';
  let { projectId, sessionId, run }: { projectId: string; sessionId: string; run: string } =
    $props();
  let view = $state<PollState<WatchedRun>>(initialPoll());
  const scope = untrack(() => `activity:${projectId}:${sessionId}:${run}`);
  const kept = untrack(() => browserState.load(scope).draft ?? {});
  let category = $state(kept.category ?? 'all');
  let search = $state(kept.search ?? '');
  let selected = $state(kept.selected ?? '');
  $effect(() => {
    browserState.save(scope, { category, search, selected });
  });
  const events = $derived(
    view.data?.events.filter(
      (event) =>
        (category === 'all' || event.kind === category) &&
        JSON.stringify(event.data).toLocaleLowerCase().includes(search.toLocaleLowerCase()),
    ) ?? [],
  );
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
  <label
    >Event category <select bind:value={category}
      ><option value="all">All</option
      >{#each ['started', 'user', 'model', 'assistant', 'tool', 'notice', 'waiting', 'fold', 'stopped'] as kind}<option
          value={kind}>{kind}</option
        >{/each}</select
    ></label
  >
  <label>Search loaded events <input type="search" bind:value={search} /></label>
  {#if view.data}
    <ol>
      {#each events as event (event.seq)}<li>
          <button
            aria-expanded={selected === String(event.seq)}
            onclick={() => (selected = selected === String(event.seq) ? '' : String(event.seq))}
            >{event.seq} · {event.kind} · {formatTime(event.at)}</button
          >{#if selected === String(event.seq)}<pre>{visible(
                JSON.stringify(event.data, null, 2),
              )}</pre>{/if}
        </li>{:else}<li>No loaded events match.</li>{/each}
    </ol>
    <details>
      <summary>Rendered conversation</summary><ChatRun run={view.data} {projectId} />
    </details>
  {/if}
</div>
