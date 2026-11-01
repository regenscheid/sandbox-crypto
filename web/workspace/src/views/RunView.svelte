<!-- SPDX-License-Identifier: MIT -->
<!-- A run's conversation: the task, the agent's messages and its tool calls, the answer;
     and the MCP traffic the policy middleware inspected for it. -->
<script lang="ts">
  import type { RunRecord } from '../lib/api';
  import { duration, formatTime } from '../lib/format';
  import Markdown from '../lib/Markdown.svelte';
  import HiddenBanner from './HiddenBanner.svelte';
  import { stoppedText } from '../lib/chat';
  import { entries, toolMixLine, usageLine } from '../lib/transcript';
  import RunFolds from './RunFolds.svelte';
  import RunSkills from './RunSkills.svelte';
  import ContextInspector from './ContextInspector.svelte';
  import ToolCall from './ToolCall.svelte';
  import TrafficSection from './TrafficSection.svelte';

  let { run, runName }: { run: RunRecord; runName: string } = $props();
  let contextOpen = $state(false);
  const steps = $derived(entries(run));
  const calls = $derived(run.stored_history?.collections?.tools ?? run.tools.length);
  const paged = $derived(
    (run.stored_history?.collections?.messages ?? run.messages.length) > run.messages.length ||
      calls > run.tools.length,
  );
  const historyBase = $derived(
    run.stored_history
      ? `/api/v0/projects/${encodeURIComponent(run.context.project_id)}/sessions/${encodeURIComponent(run.context.session_id)}/runs/${encodeURIComponent(run.stored_history.run)}`
      : '',
  );
  const usage = $derived(usageLine(run.usage));
  const toolMix = $derived(toolMixLine(run.summaries));
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
  {#if toolMix}
    <dt>Computation</dt>
    <dd>{toolMix}</dd>
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
      <span class="problem">
        {(run.answer ?? run.final_text) === null ? 'without an answer' : 'with an answer'}. {stoppedText(
          {
            stopped: run.stopped,
          },
        )}
      </span>
    {/if}
  </dd>
</dl>

<button type="button" onclick={() => (contextOpen = true)}>Context management…</button>
{#if contextOpen}<ContextInspector {run} {runName} close={() => (contextOpen = false)} />{/if}

{#if paged}
  <p class="notice">
    This run's full history is stored separately from its bounded inline preview.
    <a href={`${historyBase}/evidence`} download>Download exact stored evidence</a>
    · <a href={`${historyBase}/history?collection=messages&limit=20`}>Read a history page</a>
  </p>
  {#if steps.length === 0 && (run.answer ?? run.final_text)}
    <section class="message assistant final">
      <h3>Answer preview</h3>
      <Markdown source={run.answer ?? run.final_text ?? ''} /><HiddenBanner
        text={run.answer ?? run.final_text ?? ''}
        links
      />
    </section>
  {/if}
{/if}

<RunSkills {run} />
<RunFolds {run} />
<TrafficSection
  projectId={run.context.project_id}
  sessionId={run.context.session_id}
  run={runName}
/>

{#if run.stopped === 'error'}
  <p class="problem">Partial transcript. No final answer was recorded.</p>
{/if}

{#each steps as step, index (index)}
  {#if step.kind === 'system'}
    <details class="message system">
      <summary>System prompt</summary>
      <Markdown source={step.text} /><HiddenBanner text={step.text} links />
    </details>
  {:else if step.kind === 'user'}
    <section class="message user">
      <h3>Task</h3>
      <Markdown source={step.text} /><HiddenBanner text={step.text} links />
    </section>
  {:else}
    <section class="message assistant" class:final={step.final}>
      <h3>{step.final ? 'Answer' : 'Agent'}</h3>
      {#if step.text}
        <Markdown source={step.text} /><HiddenBanner text={step.text} links />
      {/if}
      {#each step.tools as tool, number (number)}
        <ToolCall step={tool} />
      {/each}
    </section>
  {/if}
{/each}
