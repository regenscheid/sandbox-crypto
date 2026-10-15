<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { formatTime } from '../lib/format';
  import { KERNEL_POLL, readKernels, type KernelObservation } from '../lib/kernels';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  let {
    projectId,
    sessionId,
    environmentId,
    initialState = initialPoll(),
  }: {
    projectId: string;
    sessionId: string;
    environmentId: string;
    initialState?: PollState<KernelObservation>;
  } = $props();
  let viewState = $state<PollState<KernelObservation>>(untrack(() => initialState));
  let mounted = $state(false);
  onMount(() => {
    mounted = true;
  });
  $effect(() => {
    if (!mounted) return;
    const scope = { projectId, sessionId, environmentId };
    viewState = initialPoll();
    const poller = new Poller(
      (signal) => readKernels(scope, signal),
      (value) => {
        viewState = value;
      },
      KERNEL_POLL,
    );
    return poller.watch();
  });
</script>

<section aria-label="Session kernels">
  <h2>Session kernels</h2>
  <p class="muted">
    Passive observations for this session. Revisions do not prove which modules are loaded.
  </p>
  {#if viewState.phase === 'loading'}
    <p role="status">Observing existing kernels…</p>
  {:else if viewState.phase === 'paused'}
    <p role="status">Kernel observations paused while this page is hidden.</p>
  {:else if viewState.phase === 'signed_out'}
    <p role="status">Kernel observations stopped for sign-out. Reload after signing in.</p>
  {:else if viewState.phase === 'missing'}
    <p role="status">This session is absent or closed. Kernel observations stopped.</p>
  {:else if viewState.phase === 'unsupported'}
    <p role="status">
      Kernel observations unavailable: the worker is unconfigured or does not support passive
      observations.
    </p>
  {:else if viewState.phase === 'unavailable'}
    <p role="status">Kernel observations unavailable. The current kernel state is unknown.</p>
  {/if}
  {#if viewState.data}
    {#if viewState.phase !== 'observed'}<p>
        <strong>Previous observation; current state is unknown.</strong>
      </p>{/if}
    <p class="muted">
      Worker receipt time: <time datetime={viewState.data.observed_at}
        >{formatTime(viewState.data.observed_at)}</time
      >. This is an observation time, not an atomic state or freshness guarantee.
    </p>
    <p>
      Environment revision observed: <code>{viewState.data.current_revision ?? 'unknown'}</code>
    </p>
    {#if viewState.data.kernels.length === 0}
      <p>
        {viewState.phase === 'observed'
          ? 'No kernels were observed in this connected session.'
          : 'No kernels were present in the previous observation.'}
      </p>
    {:else}
      <ul>
        {#each viewState.data.kernels as kernel (kernel.kernel_id)}
          <li>
            <strong>{kernel.language}: {kernel.revision_status}</strong> ·
            <code>{kernel.kernel_id}</code>
            {#if kernel.run}· run <code>{kernel.run}</code>{/if}
            <p>
              Generation <code>{kernel.generation}</code>; start revision
              <code>{kernel.start_revision ?? 'unknown'}</code>; current revision
              <code>{kernel.current_revision ?? 'unknown'}</code>.
            </p>
            {#if kernel.revision_status === 'predates'}<p>
                This kernel predates the observed environment revision.
              </p>{/if}
            {#if kernel.revision_status === 'unknown'}<p>
                The kernel's revision relationship is unknown.
              </p>{/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>
