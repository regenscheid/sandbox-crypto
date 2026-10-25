<!-- SPDX-License-Identifier: MIT -->
<!-- One run's part of the conversation, from its events (RS-14): the messages and replies,
     with the steps between them folded (conversation.ts), and the run's questions where it
     asked them. While the run is `live`, its latest steps show what runs now. -->
<script lang="ts" generics="Q extends { tool_call: string; question_id: string }">
  import type { Snippet } from 'svelte';
  import {
    awaitingApproval,
    chatEntries,
    isContinuation,
    placeQuestions,
    thinking,
    type WatchedRun,
  } from '../lib/chat';
  import { clock, foldTranscript, liveWork } from '../lib/conversation';
  import { agentDirectory } from '../lib/agentMessages';
  import type { RunTree, TreeRun } from '../lib/api-types';
  import AgentMessageRow from './AgentMessageRow.svelte';
  import ChildOutcome from './ChildOutcome.svelte';
  import Markdown from '../lib/Markdown.svelte';
  import { messageHref } from '../lib/router';
  import { splitAttachments } from '../lib/uploads';
  import BackgroundRow from './BackgroundRow.svelte';
  import FileChips from './FileChips.svelte';
  import HiddenBanner from './HiddenBanner.svelte';
  import Icon from './Icon.svelte';
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
    continuations = new Set(),
    sessionId = null,
    tree = null,
    role = 'main',
    showAgent = null,
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
    /** The IDs of the session's automatic continuation messages, shown folded. */
    continuations?: ReadonlySet<string>;
    /** The run's session, for its agents' messages (B10, item 2). */
    sessionId?: string | null;
    /** The session's run tree: its agents' names and its children's outcomes. */
    tree?: RunTree | null;
    /** The run's role until the tree lists it: the chat panel shows main runs. */
    role?: TreeRun['role'] | null;
    /** Opens a delegation's card in the session's Agents panel. */
    showAgent?: ((delegation: string) => void) | null;
  } = $props();
  const agents = $derived(agentDirectory(tree, run.run, role));
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
  /** The work group the run is on now, if any: the last, with only background rows after. */
  const liveIndex = $derived(live ? liveWork(items) : -1);
</script>

<ol class="transcript">
  {#each items as item, index (item.seq + item.kind)}
    {#if item.kind === 'user' && isContinuation(item.text, item.message, continuations)}
      <!-- The platform's own prompt to go on with an open plan: folded, as steps are. -->
      <li class="continuation">
        <details>
          <summary
            ><Icon name="chevron" size={14} class="chevron" /><span>Continuation</span></summary
          >
          <div class="continuation-text">
            <Markdown source={item.text} pins={item.pins} {projectId} />
          </div>
        </details>
      </li>
    {:else if item.kind === 'user'}
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
          {agents.self}{#if clock(item.at)}&nbsp;· {clock(item.at)}{/if}
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
          live={index === liveIndex}
          thinking={thinking(run)}
          approval={awaitingApproval(run)}
          {now}
          {subagents}
          {showAgents}
        />
      </li>
    {:else if item.kind === 'background'}
      <li><BackgroundRow {item} {live} {now} /></li>
    {:else if item.kind === 'agent-messages'}
      <li><AgentMessageRow rows={item.rows} at={item.at} {agents} {projectId} {sessionId} /></li>
    {:else if item.kind === 'child-outcomes'}
      {#each item.delegations as delegation (delegation)}
        <li>
          {#if projectId && sessionId}<ChildOutcome
              {projectId}
              {sessionId}
              delegationId={delegation}
              {tree}
              {showAgent}
            />{/if}
        </li>
      {/each}
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
  {#if live && liveIndex === -1}
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
