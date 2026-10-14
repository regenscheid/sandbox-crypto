<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api, type PatchPreview } from '../lib/api';
  import { documentRequest, documentAnswered } from '../lib/documentRequests';
  import { describeAuthor } from '../lib/authors';
  import ErrorMessage from './ErrorMessage.svelte';
  let {
    projectId,
    documentId,
    patchId,
    refreshed,
  }: { projectId: string; documentId: string; patchId: string; refreshed: () => void } = $props();
  let preview = $state<PatchPreview | null>(null);
  let busy = $state(false);
  let error = $state<unknown>(null);
  let message = $state('');
  async function load() {
    busy = true;
    error = null;
    message = '';
    try {
      preview = await api.previewPatch(projectId, documentId, patchId);
    } catch (caught) {
      error = caught;
      preview = null;
    } finally {
      busy = false;
    }
  }
  async function apply() {
    if (!preview) return;
    busy = true;
    error = null;
    try {
      const body = {
        action: 'apply_patch',
        patch_id: patchId,
        base: preview.base,
        reason: `User reviewed and applied patch ${patchId}`,
      };
      const request = await documentRequest(projectId, documentId, body);
      const result = await api.changeDocument(projectId, documentId, {
        ...body,
        operation_id: request.id,
      });
      documentAnswered(request);
      message = result.message || result.outcome;
      if (['accepted', 'unchanged'].includes(result.outcome)) refreshed();
    } catch (caught) {
      error = caught;
    } finally {
      busy = false;
    }
  }
</script>

<button disabled={busy} onclick={load}>Preview patch</button>
{#if preview}
  <section class="patch-review">
    <h3>Review patch by {describeAuthor(preview.patch.author)} against revision {preview.base}</h3>
    <p>
      Applying keeps the patch author's attribution and records your acceptance. Only its named
      operations apply.
    </p>
    <details>
      <summary>Retained operations</summary>
      <pre>{JSON.stringify(preview.patch.operations, null, 2)}</pre>
    </details>
    {#if preview.message}<p role="alert">{preview.message}</p>{/if}
    <div class="comparison">
      <div>
        <h4>Current text</h4>
        <pre>{preview.current}</pre>
      </div>
      <div>
        <h4>Proposed text</h4>
        <pre>{preview.proposed ??
            'The patch cannot safely apply. Its named blocks may be missing or it changes unnamed blocks.'}</pre>
      </div>
    </div>
    <button disabled={busy || !preview.allowed} onclick={apply}
      >Apply reviewed patch to revision {preview.base}</button
    >
    <button disabled={busy} onclick={() => (preview = null)}>Close preview</button>
  </section>
{/if}
{#if message}<p role="status">{message}</p>{/if}
{#if error}<ErrorMessage {error} />{/if}

<style>
  .patch-review {
    padding: 0.8rem;
    border: 1px solid var(--border, #9996);
  }
  .comparison {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 28rem;
    overflow: auto;
  }
</style>
