<!-- SPDX-License-Identifier: MIT -->
<!--
  The steps between two messages, folded: "Worked for 13 min · 9 steps". Opened, each step
  shows its time and line, and opened again its calls with their arguments and results, as
  text. While the run is still on these steps (`live`), it shows what runs now instead: the
  latest calls with their states, open until the user closes it.
-->
<script lang="ts">
  import {
    callSummary,
    clock,
    elapsed,
    stepText,
    stepTools,
    workedFor,
    workSummary,
    type WorkStep,
  } from '../lib/conversation';
  import { visible, visibleOutput } from '../lib/text';
  import Icon from './Icon.svelte';

  let {
    from,
    to,
    steps,
    live = false,
    thinking = false,
    now = Date.now(),
    subagents = null,
    showAgents = null,
  }: {
    from: string;
    to: string;
    steps: readonly WorkStep[];
    live?: boolean;
    /** A model call is in progress. */
    thinking?: boolean;
    now?: number;
    /** "1 active subagent", when any run. */
    subagents?: string | null;
    showAgents?: (() => void) | null;
  } = $props();
  let liveOpen = $state(true);
  /** The latest calls, oldest first, each with when its step began. */
  const recent = $derived(
    steps.flatMap((step) => step.calls.map((call) => ({ call, at: step.at }))).slice(-6),
  );
  const runningCall = $derived(recent.findLast((item) => !item.call.done));
  const summary = $derived(
    runningCall
      ? `running ${visible(runningCall.call.name)} (${elapsed(runningCall.at, now)})`
      : thinking
        ? 'thinking'
        : 'working',
  );
  const failed = (outcome: string) => /fail|error|timed?\s?out|refused/i.test(outcome);
</script>

{#if live}
  <section class="live-card" aria-label="What the agent is doing">
    <div class="live-head">
      <button type="button" aria-expanded={liveOpen} onclick={() => (liveOpen = !liveOpen)}>
        <span class="live-dot" aria-hidden="true"></span>
        <strong>Working</strong>
        <span class="muted">· {summary}</span>
        <Icon name="chevron" size={14} class="chevron" />
      </button>
      {#if subagents && showAgents}<button type="button" class="link-button" onclick={showAgents}
          >{subagents}</button
        >{/if}
    </div>
    {#if liveOpen && (recent.length || thinking)}
      <ol class="live-calls">
        {#each recent as { call, at }, index (index)}
          <li>
            <span class="step-time">{clock(at)}</span>
            <code>{visible(call.name)}</code>
            <span class="live-what">{callSummary(call)}</span>
            {#if !call.done}<span class="pill running">running {elapsed(at, now)}</span
              >{:else if failed(call.outcome)}<span class="pill failed"
                >{visible(call.outcome)}</span
              >{:else}<span class="pill">done</span>{/if}
          </li>
        {/each}
        {#if thinking}<li>
            <span class="step-time">—</span><span class="live-what">The agent is thinking…</span
            ><span class="pill waiting">next</span>
          </li>{/if}
      </ol>
    {/if}
  </section>
{:else}
  <details class="work-card">
    <summary class="work-toggle">
      <Icon name="chevron" size={14} class="chevron" />
      <strong>{workedFor(from, to)}</strong>
      <span class="muted">{workSummary(steps)}</span>
    </summary>
    <ol class="work-steps">
      {#each steps as step (step.seq)}
        <li>
          <details>
            <summary class="step-toggle">
              <span class="step-time">{clock(step.at)}</span>
              <span class="step-text">{stepText(step)}</span>
              <span class="step-tools">{stepTools(step)}</span>
            </summary>
            <div class="step-calls">
              {#each step.calls as call, n (n)}
                <div class="call-card">
                  <div class="call-head">
                    <code>{visible(call.name)}</code><span class="muted"
                      >{call.done
                        ? call.outcome
                          ? ` · ${visible(call.outcome)}`
                          : ''
                        : ' · no result recorded'}</span
                    >
                  </div>
                  {#if call.arguments}<pre class="call-args">{visible(call.arguments)}</pre>{/if}
                  {#if call.done}<pre class="call-result">{visibleOutput(call.result)}</pre>{/if}
                </div>
              {/each}
            </div>
          </details>
        </li>
      {/each}
    </ol>
  </details>
{/if}
