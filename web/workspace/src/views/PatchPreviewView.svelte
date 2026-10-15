<!-- SPDX-License-Identifier: MIT -->
<!-- A retained patch as the user reviews it before applying it (LD-19, LD-20; threat
     model, A3): the document's text after it, exactly as it would be saved, each hidden
     character a code point, with its differences from the current revision; a rendered
     preview apart. Only props, so a test renders it on the server. -->
<script lang="ts">
  import type { PatchPreview } from '../lib/api';
  import { describeAuthor } from '../lib/authors';
  import { visible } from '../lib/text';
  import ApprovalText from './ApprovalText.svelte';

  let {
    preview,
    busy,
    onapply,
    onclose,
  }: { preview: PatchPreview; busy: boolean; onapply: () => void; onclose: () => void } = $props();
</script>

<section class="patch-review">
  <h3>Review patch by {describeAuthor(preview.patch.author)} against revision {preview.base}</h3>
  <p>
    Applying keeps the patch author's attribution and records your acceptance. Only its named
    operations apply.
  </p>
  <details>
    <summary>Retained operations</summary>
    <pre>{visible(JSON.stringify(preview.patch.operations, null, 2))}</pre>
  </details>
  {#if preview.message}<p role="alert">{preview.message}</p>{/if}
  {#if preview.proposed === null}
    <p role="alert">
      The patch cannot safely apply. Its named blocks may be missing or it changes unnamed blocks.
    </p>
  {:else}
    <ApprovalText
      text={preview.proposed}
      before={preview.current}
      preview
      label="The text after the patch, exactly as it would be saved"
    />
  {/if}
  <button disabled={busy || !preview.allowed} onclick={onapply}
    >Apply reviewed patch to revision {preview.base}</button
  >
  <button disabled={busy} onclick={onclose}>Close preview</button>
</section>

<style>
  .patch-review {
    padding: 0.8rem;
    border: 1px solid var(--border, #9996);
  }
</style>
