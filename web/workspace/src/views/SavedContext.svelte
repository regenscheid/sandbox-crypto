<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api, ApiError, type RunRecord } from '../lib/api';
  import ContextInspector from './ContextInspector.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  let {
    project,
    session,
    run,
    compact = false,
    primary = false,
    label = 'Context management…',
    tab = 'timeline',
    scope = 'attempt',
  }: {
    project: string;
    session: string;
    run: string;
    /** A quiet link-like button, as in an agent's card. */
    compact?: boolean;
    /** The page's main action, as in the Context tab. */
    primary?: boolean;
    label?: string;
    /** Where the inspector opens: its section, and this attempt or the whole agent. */
    tab?: 'timeline' | 'folds' | 'context' | 'history';
    scope?: 'attempt' | 'agent';
  } = $props();
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

<button
  type="button"
  class:link-button={compact}
  class:primary-button={primary}
  aria-disabled={loading}
  aria-busy={loading}
  onclick={() => void open()}>{loading ? 'Loading context…' : label}</button
>
{#if error}<ErrorMessage {error} />{/if}
{#if opened}<ContextInspector
    run={record}
    runName={run}
    {project}
    {session}
    initialTab={tab}
    initialScope={scope}
    close={() => (opened = false)}
  />{/if}
