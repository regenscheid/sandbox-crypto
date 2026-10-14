<!-- SPDX-License-Identifier: MIT -->
<!-- One run's transcript in the chat panel, from its events (RS-14). -->
<script lang="ts">
  import { chatEntries, type WatchedRun } from '../lib/chat';
  import Markdown from '../lib/Markdown.svelte';
  let { run }: { run: WatchedRun } = $props();
  const entries = $derived(chatEntries(run.events));
</script>

<ol class="transcript">
  {#each entries as entry (entry.seq)}
    <li>
      {#if entry.kind === 'user'}
        <h3>You</h3>
        <Markdown source={entry.text} />
      {:else if entry.kind === 'assistant'}
        <h3>Agent</h3>
        {#if entry.text}<Markdown source={entry.text} />{/if}
        {#if entry.usage}<p class="muted">{entry.usage}</p>{/if}
        {#each entry.calls as call, index (index)}
          <details>
            <summary>Calls <code>{call.name}</code></summary>
            <pre>{call.arguments}</pre>
          </details>
        {/each}
      {:else if entry.kind === 'tool'}
        <details>
          <summary><code>{entry.name}</code>: {entry.outcome}</summary>
          <pre>{entry.result}</pre>
        </details>
      {:else if entry.kind === 'note'}
        <p class:problem={entry.problem} class="muted">{entry.text}</p>
      {:else}
        <p class="muted">{entry.text}</p>
      {/if}
    </li>
  {/each}
</ol>

<style>
  .transcript {
    list-style: none;
    padding: 0;
  }
  .transcript li {
    margin: 0.6rem 0;
  }
  .transcript h3 {
    font-size: 1rem;
    margin: 0;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 20rem;
    overflow: auto;
  }
  .problem {
    color: var(--problem, #b3261e);
  }
</style>
