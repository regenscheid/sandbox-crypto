<!-- SPDX-License-Identifier: MIT -->
<!-- The files a message attached, each by its name and size; the name is shown as text. -->
<script lang="ts">
  import type { AttachedFile } from '../lib/uploads';
  import { sizeText } from '../lib/pdfAnchors';
  import { visible } from '../lib/text';
  import Icon from './Icon.svelte';
  let { files }: { files: readonly AttachedFile[] } = $props();
</script>

{#if files.length}
  <ul class="file-chips" aria-label="Attached files">
    {#each files as file, index (index)}
      <li class="file-chip" title={file.path ? visible(file.path) : undefined}>
        <Icon name="document" size={15} />
        <span>{visible(file.filename)}</span>
        <small>{sizeText(file.size)}</small>
      </li>
    {/each}
  </ul>
{/if}
