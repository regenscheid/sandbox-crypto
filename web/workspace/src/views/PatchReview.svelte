<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api, type PatchPreview } from '../lib/api';
  import { describeAuthor } from '../lib/authors';
  import { Detail, noDetail } from '../lib/detail';
  import { applyPatch, patchApplied } from '../lib/documentHistory';
  import { guardedWrite } from '../lib/mutation';
  import { idle, submit } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  let {
    projectId,
    documentId,
    patchId,
    refreshed,
  }: { projectId: string; documentId: string; patchId: string; refreshed: () => void } = $props();
  const shown = $state(noDetail<string, PatchPreview>());
  const previews = new Detail(shown, (patch) => api.previewPatch(projectId, documentId, patch));
  const applying = $state(idle());
  let message = $state('');
  const preview = $derived(shown.value);
  const busy = $derived(shown.loading || applying.busy);
  const error = $derived(applying.error ?? shown.error);
  function load() {
    message = '';
    applying.error = null;
    void previews.show(patchId);
  }
  async function apply() {
    const reviewed = shown.value;
    if (!reviewed) return;
    await submit(applying, async () => {
      const result = await guardedWrite(
        'document',
        `${projectId}:${documentId}`,
        applyPatch(reviewed),
        (change) => api.changeDocument(projectId, documentId, change),
      );
      message = result.message || result.outcome;
      if (patchApplied(result)) {
        previews.clear(); // its preview no longer describes the document
        refreshed();
      }
    });
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
    <button disabled={busy} onclick={() => previews.clear()}>Close preview</button>
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
