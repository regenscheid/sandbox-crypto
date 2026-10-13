<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { api } from '../lib/api';
  import { formatTime } from '../lib/format';
  import { KernelPoller, onKernelLogout, type KernelPollState } from '../lib/kernels';
  let { projectId, sessionId, environmentId, initialState = {phase: 'loading', data: null} }:
    {projectId: string; sessionId: string; environmentId: string; initialState?: KernelPollState} = $props();
  let viewState = $state<KernelPollState>(untrack(() => initialState));
  let mounted = $state(false);
  onMount(() => { mounted = true; });
  $effect(() => {
    if (!mounted) return;
    const project = projectId, session = sessionId, environment = environmentId;
    viewState = {phase: 'loading', data: null};
    const poller = new KernelPoller(signal => api.kernels(project, session, environment, signal), value => { viewState = value; });
    const visibility = () => poller.setVisible(document.visibilityState === 'visible');
    const offLogout = onKernelLogout(() => poller.logout());
    document.addEventListener('visibilitychange', visibility);
    visibility();
    return () => { poller.stop(); offLogout(); document.removeEventListener('visibilitychange', visibility); };
  });
</script>

<section aria-label="Session kernels">
  <h2>Session kernels</h2>
  <p class="muted">Passive observations for this session. Revisions do not prove which modules are loaded.</p>
  {#if viewState.phase === 'loading'}
    <p role="status">Observing existing kernels…</p>
  {:else if viewState.phase === 'paused'}
    <p role="status">Kernel observations paused while this page is hidden.</p>
  {:else if viewState.phase === 'signed_out'}
    <p role="status">Kernel observations stopped for sign-out. Reload after signing in.</p>
  {:else if viewState.phase === 'missing'}
    <p role="status">This session is absent or closed. Kernel observations stopped.</p>
  {:else if viewState.phase === 'unconfigured'}
    <p role="status">Kernel observations unavailable: the worker is unconfigured or does not support passive observations.</p>
  {:else if viewState.phase === 'unavailable'}
    <p role="status">Kernel observations unavailable. The current kernel state is unknown.</p>
  {/if}
  {#if viewState.data}
    {#if viewState.phase !== 'observed'}<p><strong>Previous observation; current state is unknown.</strong></p>{/if}
    <p class="muted">
      Worker receipt time: <time datetime={viewState.data.observed_at}>{formatTime(viewState.data.observed_at)}</time>.
      This is an observation time, not an atomic state or freshness guarantee.
    </p>
    <p>Environment revision observed: <code>{viewState.data.current_revision ?? 'unknown'}</code></p>
    {#if viewState.data.kernels.length === 0}
      <p>{viewState.phase === 'observed' ? 'No kernels were observed in this connected session.' : 'No kernels were present in the previous observation.'}</p>
    {:else}
      <ul>
        {#each viewState.data.kernels as kernel (kernel.kernel_id)}
          <li>
            <strong>{kernel.language}: {kernel.revision_status}</strong> · <code>{kernel.kernel_id}</code>
            <p>Generation <code>{kernel.generation}</code>; start revision <code>{kernel.start_revision ?? 'unknown'}</code>;
              current revision <code>{kernel.current_revision ?? 'unknown'}</code>.</p>
            {#if kernel.revision_status === 'predates'}<p>This kernel predates the observed environment revision.</p>{/if}
            {#if kernel.revision_status === 'unknown'}<p>The kernel's revision relationship is unknown.</p>{/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>
