<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api, ApiError, type RunRecord } from '../lib/api';
  import ContextInspector from './ContextInspector.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  let { project, session, run }: { project: string; session: string; run: string } = $props();
  let record = $state<RunRecord | null>(null);
  let opened = $state(false);
  let loading = $state(false);
  let error = $state<unknown>(null);
  async function open() {
    if (loading) return;
    loading = true;
    error = null;
    try {
      record = await api.run(project, session, run);
      opened = true;
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 404) {
        // A running attempt may have observations before its final run record.
        record = null;
        opened = true;
      } else error = caught;
    } finally {
      loading = false;
    }
  }
</script>

<button type="button" aria-disabled={loading} aria-busy={loading} onclick={() => void open()}
  >{loading ? 'Loading context…' : 'Context management…'}</button
>
{#if error}<ErrorMessage {error} />{/if}
{#if opened}<ContextInspector
    run={record}
    runName={run}
    {project}
    {session}
    close={() => (opened = false)}
  />{/if}
