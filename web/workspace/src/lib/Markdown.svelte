<!-- SPDX-License-Identifier: MIT -->
<!--
  The one component that shows text: Markdown with LaTeX math (markdown.ts). It renders a
  view of `source` and never changes it.
-->
<script lang="ts">
  import { renderMarkdown } from './markdown';

  let { source }: { source: string } = $props();
  const html = $derived(renderMarkdown(source));
</script>

<!-- markdown.ts escapes all HTML in the source and admits only safe links. -->
<div class="markdown">{@html html}</div>
