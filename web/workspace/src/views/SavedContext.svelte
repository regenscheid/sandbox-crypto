<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api, type RunRecord } from '../lib/api';
  import ContextInspector from './ContextInspector.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  let { project, session, run }: { project: string; session: string; run: string } = $props();
  let record = $state<RunRecord | null>(null);
  let loading = $state(false);
  let error = $state<unknown>(null);
  async function open() {
    if (loading) return;
    loading = true;
    error = null;
    try {
      record = await api.run(project, session, run);
    } catch (caught) {
      error = caught;
    } finally {
      loading = false;
    }
  }
</script>

<button type="button" aria-disabled={loading} aria-busy={loading} onclick={() => void open()}
  >{loading ? 'Loading saved context…' : 'Context management…'}</button
>
{#if error}<p class="muted">
    Context inspection requires a saved run record. Live attempts may not have one yet.
  </p>
  <ErrorMessage {error} />{/if}
{#if record}<ContextInspector run={record} runName={run} close={() => (record = null)} />{/if}
