<!-- SPDX-License-Identifier: MIT -->
<!--
  The one component that shows text: Markdown with LaTeX math (markdown.ts). It renders a
  view of `source` and never changes it. When answers arrive for model repairs of formulas
  it shows, it renders again, at most once a frame (mathrepair.ts); the page keeps KaTeX's
  results, so that costs little. KaTeX works a bounded time in one go, which the page's
  texts share: math it had no time for shows as written, and the text renders again in a
  later go, starting where the last stopped, until all of it is rendered. The mark on a
  repaired formula shows or hides why, and the formula as written.
-->
<script lang="ts">
  import { renderMarkdown, whenMathTime, type TextResults } from './markdown';
  import { pageRepairs } from './mathrepair';

  let { source }: { source: string } = $props();
  /** Counts what makes this text render again: repairs it waits for, or time for its math. */
  let arrivals = $state(0);
  let element: HTMLDivElement | undefined = $state();
  /** KaTeX's results for this text's formulas, kept while some of its math waits for time. */
  const earlier: TextResults = new Map();

  const rendered = $derived.by(() => {
    void arrivals;
    return renderMarkdown(source, pageRepairs, earlier);
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

  $effect(() => {
    // Math KaTeX had no time for in this go: the next go renders more of it.
    if (!rendered.unfinished) return;
    return whenMathTime(() => {
      arrivals += 1;
    });
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
