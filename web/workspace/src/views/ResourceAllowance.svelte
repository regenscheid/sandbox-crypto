<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { onDestroy } from 'svelte';
  import type { ResourceDecision, ResourceRequest } from '../lib/api-types';
  import { post } from '../lib/http';
  import ErrorMessage from './ErrorMessage.svelte';

  let { path, item }: { path: string; item: ResourceRequest } = $props();
  let reason = $state('Continue from the saved research checkpoint.');
  let attempted = $state(false);
  let busy = $state(false);
  let error = $state<unknown>(null);
  let result = $state<ResourceDecision | null>(null);
  const controller = new AbortController();
  let disposed = false;
  const extension = $derived(
    item.resource === 'turns'
      ? { action: 'extend_turns', label: '300 more turns' }
      : item.resource === 'elapsed_seconds'
        ? { action: 'extend_time', label: '1 more active hour' }
        : { action: 'extend_storage', label: '1 GiB more retained artifact space' },
  );
  onDestroy(() => {
    disposed = true;
    controller.abort();
  });

  async function approve() {
    if (busy || attempted || !reason.trim()) return;
    busy = true;
    attempted = true;
    const body = {
      request_id: item.request_id,
      current: item.current,
      epoch: item.epoch,
      total: item.total,
      action: extension.action,
      reason: reason.trim(),
    };
    try {
      const decision = await post<ResourceDecision>(`${path}/resources/decide`, body, {
        signal: controller.signal,
        deadlineMs: 15_000,
      });
      if (!disposed) result = decision;
    } catch (caught) {
      if (!disposed) error = caught;
    } finally {
      if (!disposed) busy = false;
    }
  }
</script>

<div class="allowance">
  {#if error}<ErrorMessage {error} />{/if}
  {#if result}
    <p role="status">
      Extension recorded. Resume research to continue; this approval did not start a run.
    </p>
  {:else if attempted}
    <p role="status">
      Approval was sent. Check the recorded decision before taking further action.
    </p>
  {:else}
    <p>
      Up to {extension.label}, bounded by project policy. The entire research tree must be idle.
      Tokens, worker capacity and execution deadlines stay unchanged.
    </p>
    <label
      >Reason for continuing<textarea bind:value={reason} maxlength="2000" rows="2"
      ></textarea></label
    >
    <button disabled={busy || !reason.trim()} onclick={approve}>Approve {extension.label}</button>
  {/if}
</div>

<style>
  .allowance {
    margin-top: 0.65rem;
    padding: 0.75rem;
    border-left: 2px solid var(--border);
  }
  label {
    display: grid;
    gap: 0.35rem;
  }
  textarea {
    width: 100%;
    box-sizing: border-box;
  }
  button {
    margin-top: 0.5rem;
  }
</style>
