<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  let { text }: { text: string } = $props();
</script>
<h3>Pinned evidence preview</h3>
{#if text === ''}<p>Empty source (0 characters).</p>{/if}
<pre aria-label="Pinned evidence preview">{text}</pre>
<style>
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 22rem; overflow: auto; }
</style>
