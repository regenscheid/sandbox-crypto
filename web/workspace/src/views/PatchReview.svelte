<!-- SPDX-License-Identifier: MIT -->
<!-- A retained patch's preview, read on request, and the user's application of it; the
     preview itself is PatchPreviewView. -->
<script lang="ts">
  import { api, type PatchPreview } from '../lib/api';
  import { Detail, noDetail } from '../lib/detail';
  import { applyPatch, patchApplied } from '../lib/documentHistory';
  import { guardedWrite } from '../lib/mutation';
  import { idle, submit } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  import PatchPreviewView from './PatchPreviewView.svelte';
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
  <PatchPreviewView
    {preview}
    {busy}
    onapply={() => void apply()}
    onclose={() => previews.clear()}
  />
{/if}
{#if message}<p role="status">{message}</p>{/if}
{#if error}<ErrorMessage {error} />{/if}
