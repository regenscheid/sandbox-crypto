<!-- SPDX-License-Identifier: MIT -->
<!--
  Background work in one line of the transcript (B10, item 5): a wait on jobs or subagents,
  "2 finished · 16 s" or "1 running · 42 s so far", or notices of work that ended; opened,
  each job, subagent or kernel with its state. The worker reports no memory, CPU or disk
  for a compute panel yet, so none is shown.
-->
<script lang="ts">
  import {
    backgroundList,
    backgroundText,
    backgroundWhat,
    type BackgroundItem,
  } from '../lib/conversation';
  import Icon from './Icon.svelte';

  let {
    item,
    live = false,
    now = Date.now(),
  }: { item: BackgroundItem; live?: boolean; now?: number } = $props();
</script>

<details class="background-row">
  <summary>
    <Icon name="chevron" size={14} class="chevron" />
    <span>{backgroundWhat(item)}</span>
    <span>· {backgroundText(item, now, live)}</span>
  </summary>
  <ul>
    {#each backgroundList(item, live) as one (one.id)}
      <li><code>{one.id}</code> · {one.state}</li>
    {/each}
  </ul>
</details>
