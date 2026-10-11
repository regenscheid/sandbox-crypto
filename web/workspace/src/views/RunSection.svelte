<!-- SPDX-License-Identifier: MIT -->
<!-- One run in a session's history; its record is fetched the first time it is opened. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, type RunEntry, type RunRecord } from '../lib/api';
  import { formatTime } from '../lib/format';
  import ErrorMessage from './ErrorMessage.svelte';
  import RunView from './RunView.svelte';

  let {
    projectId,
    sessionId,
    run,
    initiallyOpen,
  }: { projectId: string; sessionId: string; run: RunEntry; initiallyOpen: boolean } = $props();

  let open = $state(untrack(() => initiallyOpen));
  let record: Promise<RunRecord> | undefined;

  function load(): Promise<RunRecord> {
    record ??= api.run(projectId, sessionId, run.name);
    return record;
  }
</script>

<details class="run" bind:open>
  <summary>
    Run <code>{run.run_id}</code> <span class="muted">started {formatTime(run.started_at)}</span>
  </summary>
  {#if open}
    {#await load()}
      <p class="muted">Loading…</p>
    {:then found}
      <RunView run={found} />
    {:catch error}
      <ErrorMessage {error} />
    {/await}
  {/if}
</details>
