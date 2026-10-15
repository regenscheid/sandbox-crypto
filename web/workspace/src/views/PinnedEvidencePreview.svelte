<!-- SPDX-License-Identifier: MIT -->
<!-- A pinned evidence text as read, raw: never rendered, each character a person may not
     see shown as its code point. -->
<script lang="ts">
  import { visible } from '../lib/text';

  let { text }: { text: string } = $props();
</script>

<h3>Pinned evidence preview</h3>
{#if text === ''}<p>Empty source (0 characters).</p>{/if}
<pre aria-label="Pinned evidence preview">{visible(text)}</pre>

<style>
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 22rem;
    overflow: auto;
  }
</style>
