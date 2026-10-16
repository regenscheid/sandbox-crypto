<!-- SPDX-License-Identifier: MIT -->
<!-- One chart where the text writes it (figures, FG-1, FG-15, FG-17, FG-18): drawn in the
     figure frame from its pinned data, or, when a data path is not pinned, its source with
     each path and why; past the text's limit, its source and a button that draws it. Its
     source is always offered. -->
<script lang="ts">
  import type { PlacedChart } from '../lib/figureRefs';
  import { visible } from '../lib/text';
  import FigureFrame from './FigureFrame.svelte';

  let { projectId, chart }: { projectId: string; chart: Omit<PlacedChart, 'target'> } = $props();
  // svelte-ignore state_referenced_locally
  let drawing = $state(chart.drawn);
</script>

<span class="figure-chart" role="figure">
  {#if chart.broken.length}
    <span class="problem">
      Not drawn: {#each chart.broken as item, index (index)}{index ? '; ' : ''}<bdi
          >{visible(item.path)}</bdi
        >
        ({item.reason}){/each}.
    </span>
  {:else if drawing}
    <FigureFrame {projectId} chart={{ source: chart.source, data: chart.data }} alt="Chart" />
  {:else}
    <button type="button" onclick={() => (drawing = true)}>Draw this chart</button>
  {/if}
  <details open={chart.broken.length > 0 || !drawing}>
    <summary>The chart's source</summary>
    <pre><code>{visible(chart.source)}</code></pre>
  </details>
</span>

<style>
  .figure-chart {
    display: block;
    margin: 0.5rem 0;
  }
</style>
