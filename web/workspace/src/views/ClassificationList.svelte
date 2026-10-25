<!-- SPDX-License-Identifier: MIT -->
<!-- The project's Classifications list (docs/specs/workspace-browsers.md, CL-1): each
     configured service's classification in force, opening its Classification tab. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { ClassificationInForce } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId, onopen }: { projectId: string; onopen: (server: string) => void } = $props();
  let found = $state<ClassificationInForce[] | null>(null);
  let error = $state<unknown>(null);

  async function load() {
    try {
      const answer = await api.classifications(projectId);
      found = Array.isArray(answer?.classifications) ? answer.classifications : null;
      error = null;
    } catch (caught) {
      error = caught;
    }
  }
  void load();

  function when(item: ClassificationInForce): string {
    if (item.source.kind === 'catalog')
      return item.source.pinned ? `pinned ${item.source.pinned}` : 'not pinned';
    return item.source.accepted_at ? `accepted ${formatTime(item.source.accepted_at)}` : '';
  }
</script>

<section class="classification-list" aria-label="Classifications">
  <h2>Classifications</h2>
  {#if error}<ErrorMessage {error} />{/if}
  {#if found}
    {#each found as item (item.server)}
      <button type="button" class="classification-row" onclick={() => onopen(item.server)}>
        <strong>{visible(item.server)}</strong>
        <span
          >{item.source.kind === 'catalog'
            ? 'Checked-in file'
            : `Project revision ${item.source.revision?.slice(0, 12) ?? ''}`}</span
        >
        <span class="muted">{when(item)}</span>
        <span>{item.tools.length} tools</span>
        {#if item.drift}<span class="pill warn"
            >{item.drift.held ? 'Drift · calls held' : 'Drift'}</span
          >{:else}<span class="pill ok">No drift</span>{/if}
      </button>
    {:else}<p class="muted">No configured service has a classification here.</p>{/each}
  {/if}
</section>

<style>
  .classification-list {
    margin: 0.8rem 0;
  }
  .classification-row {
    width: 100%;
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 1rem;
    align-items: baseline;
    text-align: left;
    background: transparent;
    border: 0;
    border-bottom: 1px solid var(--border, #ccc);
    padding: 0.6rem;
    cursor: pointer;
  }
  .classification-row:hover {
    background: var(--surface-raised, #f1f4f3);
  }
</style>
