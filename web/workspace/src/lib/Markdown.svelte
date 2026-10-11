<!-- SPDX-License-Identifier: MIT -->
<!--
  The one component that shows text: Markdown with LaTeX math (markdown.ts). It renders a
  view of `source` and never changes it. When answers arrive for model repairs of formulas
  it shows, it renders again, at most once a frame (mathrepair.ts); the page keeps KaTeX's
  results, so that costs little. The mark on a repaired formula shows or hides why, and
  the formula as written.
-->
<script lang="ts">
  import { renderMarkdown } from './markdown';
  import { pageRepairs } from './mathrepair';

  let { source }: { source: string } = $props();
  /** Counts the arrivals of the repairs this text waits for, to render it again. */
  let arrivals = $state(0);
  let element: HTMLDivElement | undefined = $state();

  const rendered = $derived.by(() => {
    void arrivals;
    return renderMarkdown(source, pageRepairs);
  });

  $effect(() => {
    const waiting = rendered.waiting;
    if (waiting.size === 0) return;
    const stop = pageRepairs.subscribe((keys) => {
      for (const key of keys) {
        if (waiting.has(key)) {
          arrivals += 1;
          return;
        }
      }
    });
    // An answer that arrived before this subscription.
    if ([...waiting].some((key) => !pageRepairs.isPending(key))) arrivals += 1;
    return stop;
  });

  function toggle(event: MouseEvent): void {
    const mark = event.target instanceof Element ? event.target.closest('button.math-mark') : null;
    const details = mark?.nextElementSibling;
    if (!mark || !(details instanceof HTMLElement) || !details.classList.contains('math-details')) {
      return;
    }
    details.hidden = !details.hidden;
    mark.setAttribute('aria-expanded', String(!details.hidden));
  }

  $effect(() => {
    const target = element;
    target?.addEventListener('click', toggle);
    return () => target?.removeEventListener('click', toggle);
  });
</script>

<!-- markdown.ts escapes all HTML in the source and admits only safe links. -->
<div class="markdown" bind:this={element}>{@html rendered.html}</div>
