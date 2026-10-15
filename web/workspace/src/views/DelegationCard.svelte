<!-- SPDX-License-Identifier: MIT -->
<!-- One delegation (docs/specs/plans-and-delegation.md, PD-42): its brief, read-only (the
     platform's task, the entry it checks, leading to it at that revision, the inputs, the
     parent's note, which is not evidence, the model chosen and the limits granted), and,
     once it ended, its outcome: how it ended, what the platform observed of its reading,
     the result's verdict, status, coverage, refutation or obstacle and the rest, the
     report, and why the plan fence refused calls or the result did not validate. Agents'
     text is shown as text, each character a person may not see as its code point. Only
     props, so a test renders it on the server. -->
<script lang="ts">
  import type { Delegation } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { briefView, endedText, readingText, resultView } from '../lib/runTree';
  import { ENTRY_NAME, ledgerEntryHref } from '../lib/router';
  import { visible } from '../lib/text';

  let { projectId, delegation }: { projectId: string; delegation: Delegation } = $props();
  const brief = $derived(briefView(delegation));
  const result = $derived(resultView(delegation));
  const reading = $derived(readingText(delegation));
</script>

{#snippet list(items: readonly string[])}
  <ul class="links">
    {#each items as item, index (index)}<li class="said">{item}</li>{/each}
  </ul>
{/snippet}

<div class="delegation">
  <p>
    <strong>Delegation <code>{visible(delegation.delegation_id)}</code></strong>, a
    {visible(delegation.kind)}, started {formatTime(delegation.started_at)}
  </p>
  <details>
    <summary>Its brief, read-only</summary>
    <p class="said">{brief.task}</p>
    {#if brief.entry}
      <p>
        Checks {#if ENTRY_NAME.test(brief.entry.handle)}<a
            href={ledgerEntryHref(projectId, brief.entry.handle, brief.entry.revision ?? undefined)}
            >{brief.entry.handle}{brief.entry.revision === null
              ? ''
              : ` r${brief.entry.revision}`}</a
          >{:else}{visible(brief.entry.handle)}{/if}
      </p>
    {/if}
    {#if brief.inputs.length}<p class="label">Inputs</p>
      {@render list(brief.inputs)}{/if}
    {#if brief.note}
      <p class="label">The parent's note, which is not evidence</p>
      <p class="said">{brief.note}</p>
    {/if}
    <p class="muted">
      Model: {brief.model}{brief.limits ? `; limits granted: ${brief.limits}` : ''}
    </p>
  </details>
  <p>{endedText(delegation)}</p>
  {#if delegation.ended !== null}
    {#if delegation.ended_at}<p class="muted">Ended {formatTime(delegation.ended_at)}</p>{/if}
    {#if reading}<p>{reading}</p>{/if}
    {#if result}
      <p>
        <strong>Verdict: {result.verdict}</strong>; status {result.status}
      </p>
      {#if result.refutation}<p class="label">Refutation</p>
        <p class="said">{result.refutation}</p>{/if}
      {#if result.obstacle}<p class="label">Obstacle</p>
        <p class="said">{result.obstacle}</p>{/if}
      {#if result.summary}<p class="label">Summary</p>
        <p class="said">{result.summary}</p>{/if}
      {#if result.examined.length}<p class="label">Examined</p>
        {@render list(result.examined)}{/if}
      {#if result.notExamined.length}<p class="label">Not examined</p>
        {@render list(result.notExamined)}{/if}
      {#if result.deviations.length}<p class="label">Departures from the brief</p>
        {@render list(result.deviations)}{/if}
      {#if result.openQuestions.length}<p class="label">Open questions</p>
        {@render list(result.openQuestions)}{/if}
    {:else}
      <p class="problem">It returned no result that validated.</p>
    {/if}
    {#if delegation.errors.length}<p class="label">Why its answers did not validate</p>
      {@render list(delegation.errors.map(visible))}{/if}
    {#if delegation.refused}<p class="problem">
        The plan fence refused its calls: {visible(delegation.refused)}
      </p>{/if}
    {#if delegation.report}
      <details>
        <summary>Its report, as it returned it</summary>
        <p class="said">{visible(delegation.report)}</p>
      </details>
    {/if}
    <p class="muted">
      Ran on {delegation.alias ? visible(delegation.alias) : 'an alias it did not name'}{delegation
        .models.length
        ? ` (${delegation.models.map(visible).join(', ')})`
        : ''}{delegation.acknowledged
        ? `; its parent saw the outcome in ${visible(delegation.acknowledged)}`
        : ''}
    </p>
  {/if}
</div>

<style>
  .delegation {
    border-left: 3px solid var(--border, #9996);
    padding: 0.2em 0.6em;
    margin: 0.3em 0;
  }
  .delegation p {
    margin: 0.2em 0;
  }
  .said {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .label {
    font-size: 0.9em;
    margin-top: 0.4em;
  }
</style>
