<!-- SPDX-License-Identifier: MIT -->
<!-- A run's conversation: the task, the agent's messages and its tool calls, the answer. -->
<script lang="ts">
  import type { RunRecord } from '../lib/api';
  import { duration, formatTime } from '../lib/format';
  import Markdown from '../lib/Markdown.svelte';
  import { entries, usageLine } from '../lib/transcript';
  import ToolCall from './ToolCall.svelte';

  let { run }: { run: RunRecord } = $props();
  const steps = $derived(entries(run));
  const calls = $derived(run.tools.length);
  const usage = $derived(usageLine(run.usage));
</script>

<dl class="facts">
  <dt>Ran</dt>
  <dd>
    {formatTime(run.started_at)} for {duration(run.started_at, run.ended_at)}, {calls}
    {calls === 1 ? 'tool call' : 'tool calls'}
  </dd>
  {#if usage}
    <dt>Model</dt>
    <dd>{usage}</dd>
  {/if}
  <dt>Environment</dt>
  <dd><code>{run.context.environment_id}</code></dd>
  {#if run.context.policy_revision}
    <dt>Policy revision</dt>
    <dd><code>{run.context.policy_revision}</code></dd>
  {/if}
  <dt>Ended</dt>
  <dd>
    {#if run.stopped === 'error'}
      <span class="problem">Failed run{run.failure ? ` (${run.failure})` : ''}</span>
    {:else if run.stopped === 'answered'}
      with an answer
    {:else}
      <span class="problem">without an answer ({run.stopped})</span>
    {/if}
  </dd>
</dl>

{#if run.stopped === 'error'}
  <p class="problem">Partial transcript. No final answer was recorded.</p>
{/if}

{#each steps as step, index (index)}
  {#if step.kind === 'system'}
    <details class="message system">
      <summary>System prompt</summary>
      <Markdown source={step.text} />
    </details>
  {:else if step.kind === 'user'}
    <section class="message user">
      <h3>Task</h3>
      <Markdown source={step.text} />
    </section>
  {:else}
    <section class="message assistant" class:final={step.final}>
      <h3>{step.final ? 'Answer' : 'Agent'}</h3>
      {#if step.text}
        <Markdown source={step.text} />
      {/if}
      {#each step.tools as tool, number (number)}
        <ToolCall step={tool} />
      {/each}
    </section>
  {/if}
{/each}
