<!-- SPDX-License-Identifier: MIT -->
<!-- One run's transcript in the chat panel, from its events (RS-14). -->
<script lang="ts" generics="Q extends { tool_call: string; question_id: string }">
  import type { Snippet } from 'svelte';
  import { chatEntries, placeQuestions, type WatchedRun } from '../lib/chat';
  import Markdown from '../lib/Markdown.svelte';
  import { messageHref } from '../lib/router';
  import { visible, visibleOutput } from '../lib/text';
  import HiddenBanner from './HiddenBanner.svelte';
  let {
    run,
    projectId = null,
    questions = [],
    question,
  }: {
    run: WatchedRun;
    projectId?: string | null;
    /** The run's questions, each shown after the entry that asked it, or after the rest. */
    questions?: readonly Q[];
    question?: Snippet<[Q]>;
  } = $props();
  const entries = $derived(chatEntries(run.events));
  const placed = $derived(placeQuestions(entries, questions));
</script>

<ol class="transcript">
  {#each entries as entry (entry.seq)}
    <li
      class:user-message={entry.kind === 'user'}
      class:assistant-message={entry.kind === 'assistant'}
    >
      {#if entry.kind === 'user'}
        <h3>You</h3>
        <Markdown source={entry.text} pins={entry.pins} {projectId} />
        <HiddenBanner text={entry.text} links />
      {:else if entry.kind === 'assistant'}
        <h3>Research agent</h3>
        {#if entry.text}<Markdown source={entry.text} pins={entry.pins} {projectId} /><HiddenBanner
            text={entry.text}
            links
          />{/if}
        {#if entry.usage}<p class="muted">{entry.usage}</p>{/if}
        {#each entry.calls as call, index (index)}
          <details>
            <summary>Calls <code>{call.name}</code></summary>
            <pre>{visible(call.arguments)}</pre>
          </details>
        {/each}
      {:else if entry.kind === 'tool'}
        <details>
          <summary
            ><code>{entry.name}</code>{#if entry.outcome}: {entry.outcome}{/if}</summary
          >
          <pre>{visibleOutput(entry.result)}</pre>
        </details>
      {:else if entry.kind === 'note'}
        <p class:problem={entry.problem} class="muted">
          {entry.text}{#if entry.message && projectId}
            <a href={messageHref(projectId, entry.message)}>Its record</a>{/if}
        </p>
      {:else}
        <p class="muted">{entry.text}</p>
      {/if}
    </li>
    {#if question}
      {#each placed.after.get(entry.seq) ?? [] as asked (asked.question_id)}
        <li>{@render question(asked)}</li>
      {/each}
    {/if}
  {/each}
  {#if question}
    {#each placed.unplaced as asked (asked.question_id)}
      <li>{@render question(asked)}</li>
    {/each}
  {/if}
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
