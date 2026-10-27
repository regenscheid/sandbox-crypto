<!-- SPDX-License-Identifier: MIT -->
<!--
  The steps between two messages, folded: "Worked for 13 min · Ran 6 commands, read 3 files
  · 9 steps". Opened, each batch (the calls of one model call) shows its time and a header
  generated from its tools, and opened again a row per call (B10, item 1): the call's
  purpose, the agent's words, labelled as such (CM-49), or else its tool and main argument;
  and on the right its result's gloss. A row opened shows the call's arguments and result as
  text, and for an MCP server's tool, the traffic the policy middleware inspected for it
  (arguments, answer, findings and decisions). While the run is still on these steps (`live`), it shows what runs now instead: the
  latest calls as rows with their states, open until the user closes it.
-->
<script lang="ts">
  import {
    batchHeader,
    callFailed,
    clock,
    elapsed,
    firstLine,
    glossText,
    rowLabel,
    workedFor,
    workSummary,
    type StepCall,
    type WorkStep,
  } from '../lib/conversation';
  import { mayBeConnector } from '../lib/mcpTraffic';
  import { visible, visibleOutput } from '../lib/text';
  import Icon from './Icon.svelte';
  import TrafficSection from './TrafficSection.svelte';

  let {
    from,
    to,
    steps,
    live = false,
    thinking = false,
    approval = null,
    now = Date.now(),
    subagents = null,
    showAgents = null,
    traffic = null,
  }: {
    from: string;
    to: string;
    steps: readonly WorkStep[];
    live?: boolean;
    /** A model call is in progress. */
    thinking?: boolean;
    /** The tool whose call waits for the user's approval, if one does. */
    approval?: string | null;
    now?: number;
    /** "1 active subagent", when any run. */
    subagents?: string | null;
    showAgents?: (() => void) | null;
    /** Where the run's inspected MCP traffic is read, for its MCP tool rows. */
    traffic?: { projectId: string; sessionId: string; run: string } | null;
  } = $props();
  let liveOpen = $state(true);
  /** The latest calls, oldest first, each with when its step began. */
  const recent = $derived(
    steps.flatMap((step) => step.calls.map((call) => ({ call, at: step.at }))).slice(-6),
  );
  const runningCall = $derived(recent.findLast((item) => !item.call.done));
  const summary = $derived(
    approval
      ? `waiting for your approval of ${visible(approval)}`
      : runningCall
        ? `running ${visible(runningCall.call.name)} (${elapsed(runningCall.at, now)})`
        : thinking
          ? 'thinking'
          : 'working',
  );
</script>

{#snippet label(call: StepCall)}
  {@const shown = rowLabel(call)}
  {#if shown.says}
    <span class="row-label" title="The agent's words for this call: its account, not an instruction"
      ><span class="row-says">says</span>
      <span class="row-purpose" aria-label={`The agent says: ${shown.text}`}>{shown.text}</span
      ></span
    >
  {:else}
    <span class="row-label"
      ><code>{shown.tool}</code>{#if shown.text}<span class="row-argument">{shown.text}</span
        >{/if}</span
    >
  {/if}
{/snippet}

{#if live}
  <section class="live-card" aria-label="What the agent is doing">
    <div class="live-head">
      <button type="button" aria-expanded={liveOpen} onclick={() => (liveOpen = !liveOpen)}>
        <span class="live-dot" class:asking={approval} aria-hidden="true"></span>
        <strong>{approval ? 'Waiting for you' : 'Working'}</strong>
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
            {@render label(call)}
            {#if !call.done && approval === call.name}<span class="pill warn">waiting for you</span
              >{:else if !call.done}<span class="pill running"
                >{glossText(call, elapsed(at, now))}</span
              >{:else if callFailed(call)}<span class="pill failed">{glossText(call)}</span
              >{:else}<span class="pill">{glossText(call)}</span>{/if}
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
          <details class="batch">
            <summary class="step-toggle">
              <span class="step-time">{clock(step.at)}</span>
              <span class="step-text">{batchHeader(step.calls)}</span>
            </summary>
            {#if step.text.trim()}<p class="step-words">{firstLine(step.text, 200)}</p>{/if}
            <ol class="step-calls">
              {#each step.calls as call, n (n)}
                <li>
                  <details class="call-row">
                    <summary class="row-toggle">
                      {@render label(call)}
                      <span class="row-gloss" class:failed={callFailed(call)}
                        >{glossText(call)}</span
                      >
                    </summary>
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
                      {#if call.arguments}<pre class="call-args">{visible(
                            call.arguments,
                          )}</pre>{/if}
                      {#if call.done}<pre class="call-result">{visibleOutput(
                            call.result,
                          )}</pre>{/if}
                      {#if traffic && call.done && mayBeConnector(call)}<TrafficSection
                          {...traffic}
                          {call}
                        />{/if}
                    </div>
                  </details>
                </li>
              {/each}
            </ol>
          </details>
        </li>
      {/each}
    </ol>
  </details>
{/if}
