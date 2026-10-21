<!-- SPDX-License-Identifier: MIT -->
<!-- One run's part of the conversation, from its events (RS-14): the messages and replies,
     with the steps between them folded (conversation.ts), and the run's questions where it
     asked them. While the run is `live`, its latest steps show what runs now. -->
<script lang="ts" generics="Q extends { tool_call: string; question_id: string }">
  import type { Snippet } from 'svelte';
  import {
    awaitingApproval,
    chatEntries,
    placeQuestions,
    thinking,
    type WatchedRun,
  } from '../lib/chat';
  import { clock, foldTranscript } from '../lib/conversation';
  import Markdown from '../lib/Markdown.svelte';
  import { messageHref } from '../lib/router';
  import { splitAttachments } from '../lib/uploads';
  import FileChips from './FileChips.svelte';
  import HiddenBanner from './HiddenBanner.svelte';
  import WorkGroup from './WorkGroup.svelte';
  let {
    run,
    projectId = null,
    questions = [],
    question,
    live = false,
    now = Date.now(),
    subagents = null,
    showAgents = null,
  }: {
    run: WatchedRun;
    projectId?: string | null;
    /** The run's questions, each shown after the step that asked it, or after the rest. */
    questions?: readonly Q[];
    question?: Snippet<[Q]>;
    /** The run is working now: its last steps show as they run. */
    live?: boolean;
    now?: number;
    subagents?: string | null;
    showAgents?: (() => void) | null;
  } = $props();
  const entries = $derived(chatEntries(run.events));
  const placed = $derived(placeQuestions(entries, questions));
  const items = $derived(
    foldTranscript(entries, run.events, new Set(questions.map((asked) => asked.tool_call))),
  );
  /** Each item's questions: those its entries asked, at the last item that holds them. */
  const asked = $derived.by(() => {
    const at = new Map<number, number>();
    items.forEach((item, index) => {
      for (const seq of item.kind === 'work' ? item.steps.map((step) => step.seq) : [item.seq])
        at.set(seq, index);
    });
    const shown = new Map<number, Q[]>();
    for (const [seq, list] of placed.after) {
      const index = at.get(seq);
      if (index === undefined) continue;
      shown.set(index, [...(shown.get(index) ?? []), ...list]);
    }
    return shown;
  });
  const last = $derived(items.at(-1));
</script>

<ol class="transcript">
  {#each items as item, index (item.seq + item.kind)}
    {#if item.kind === 'user'}
      {@const split = splitAttachments(item.text)}
      <li class="bubble user-message">
        <h3 class="sr-only">You</h3>
        {#if split.text.trim()}<Markdown source={split.text} pins={item.pins} {projectId} />{/if}
        <FileChips files={split.files} />
        <HiddenBanner text={item.text} links />
      </li>
    {:else if item.kind === 'reply'}
      <li class="assistant-message">
        <h3 class="meta">
          Research agent{#if clock(item.at)}&nbsp;· {clock(item.at)}{/if}
        </h3>
        <Markdown source={item.text} pins={item.pins} {projectId} />
        <HiddenBanner text={item.text} links />
      </li>
    {:else if item.kind === 'work'}
      <li>
        <WorkGroup
          from={item.from}
          to={item.to}
          steps={item.steps}
          live={live && index === items.length - 1}
          thinking={thinking(run)}
          approval={awaitingApproval(run)}
          {now}
          {subagents}
          {showAgents}
        />
      </li>
    {:else}
      <li>
        <p class="transcript-note" class:problem={item.problem}>
          {item.text}{#if item.message && projectId}
            <a href={messageHref(projectId, item.message)}>Its record</a>{/if}
        </p>
      </li>
    {/if}
    {#if question}
      {#each asked.get(index) ?? [] as one (one.question_id)}
        <li>{@render question(one)}</li>
      {/each}
    {/if}
  {/each}
  {#if live && last?.kind !== 'work'}
    <li>
      <WorkGroup
        from=""
        to=""
        steps={[]}
        live
        thinking={thinking(run)}
        approval={awaitingApproval(run)}
        {now}
        {subagents}
        {showAgents}
      />
    </li>
  {/if}
  {#if question}
    {#each placed.unplaced as one (one.question_id)}
      <li>{@render question(one)}</li>
    {/each}
  {/if}
</ol>
