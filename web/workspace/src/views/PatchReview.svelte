<!-- SPDX-License-Identifier: MIT -->
<!-- A retained patch's preview, read on request, and the user's application of it; the
     preview itself is PatchPreviewView. An agent's suggestion may be declined instead,
     which leaves the document as it is. -->
<script lang="ts">
  import { api, type PatchPreview } from '../lib/api';
  import { Detail, noDetail } from '../lib/detail';
  import { applyPatch, declineSuggestion, patchApplied } from '../lib/documentHistory';
  import { ApiError } from '../lib/http';
  import { guardedWrite } from '../lib/mutation';
  import { idle, submit } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  import PatchPreviewView from './PatchPreviewView.svelte';
  let {
    projectId,
    documentId,
    patchId,
    suggested = false,
    refreshed,
  }: {
    projectId: string;
    documentId: string;
    patchId: string;
    /** An agent's suggestion, waiting for the user. */
    suggested?: boolean;
    refreshed: () => void;
  } = $props();
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
  async function decline() {
    await submit(applying, async () => {
      try {
        const result = await api.changeDocument(projectId, documentId, declineSuggestion(patchId));
        message = result.message || 'The suggestion was declined.';
      } catch (caught) {
        // A retry after a lost answer finds the suggestion already decided.
        if (!(caught instanceof ApiError && caught.status === 409)) throw caught;
        message = 'The suggestion is no longer waiting.';
      }
      previews.clear();
      refreshed();
    });
  }
</script>

<button disabled={busy} onclick={load}>Preview patch</button>
{#if suggested}<button disabled={busy} onclick={() => void decline()}>Decline suggestion</button
  >{/if}
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
