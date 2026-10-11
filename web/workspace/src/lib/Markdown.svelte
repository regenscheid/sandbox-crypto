<!-- SPDX-License-Identifier: MIT -->
<!--
  The one component that shows text: Markdown with LaTeX math (markdown.ts). It renders a
  view of `source` and never changes it. When answers arrive for model repairs of formulas
  it shows, it renders again, at most once a frame (mathrepair.ts); the page keeps KaTeX's
  results, so that costs little. Texts render in goes of bounded time, one after another:
  a text that comes after a go's time is spent, or that has math the go had no time for,
  waits for its turn in a later go, showing its last rendering (or itself, as written)
  meanwhile, and starts where it stopped. The mark on a repaired formula shows or hides
  why, and the formula as written. Given a `document`, the server's view of a living
  document's revision, it renders `source` as that document (markdown.ts,
  `renderDocument`), with the server's IDs and the document's macros, and reports where
  the two parsers disagree in `mismatches`.
-->
<script lang="ts">
  import { flushSync } from 'svelte';
  import type { Mismatch } from './attach';
  import { renderDocument, renderMarkdown, textState, whenMathTime } from './markdown';
  import type { DocumentInput } from './markdown';
  import { pageRepairs } from './mathrepair';

  let {
    source,
    document = null,
    mismatches = $bindable([]),
  }: { source: string; document?: DocumentInput | null; mismatches?: Mismatch[] } = $props();
  /** Counts what makes this text render again: repairs it waits for, or its turn in a go. */
  let arrivals = $state(0);
  let element: HTMLDivElement | undefined = $state();
  /** KaTeX's results for this text and its last rendering, from one rendering to the next. */
  const kept = textState();

  const rendered = $derived.by(() => {
    void arrivals;
    if (document === null) return { ...renderMarkdown(source, pageRepairs, kept), mismatches: [] };
    return renderDocument(source, document, pageRepairs, kept);
  });

  $effect(() => {
    mismatches = rendered.mismatches;
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
    if (!rendered.unfinished) return;
    // Its turn in a later go: render now, so that the go counts the time.
    return whenMathTime(() => {
      flushSync(() => {
        arrivals += 1;
      });
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
