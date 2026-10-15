<!-- SPDX-License-Identifier: MIT -->
<!--
  One tool call: its arguments, and the result the model read, all as plain text, with
  hidden characters shown as code points (text.ts, `visible`); program output drops
  terminal colour sequences and reads a carriage return as a line end (`visibleOutput`).
-->
<script lang="ts">
  import { visible, visibleOutput } from '../lib/text';
  import { argumentFields, describeResult, type ToolStep } from '../lib/transcript';

  let { step }: { step: ToolStep } = $props();
  const fields = $derived(argumentFields(step.arguments));
  const result = $derived(describeResult(step.result, step.record));
  let showRecord = $state(false);
</script>

<div class="tool">
  <p class="tool-head">
    <code class="tool-name">{visible(step.name)}</code>
    <span class="status {result.tone}">{visible(result.summary)}</span>
    {#if step.record?.replayed}
      <span class="muted">replayed: an earlier identical call's result</span>
    {/if}
  </p>

  {#each fields as field (field.name)}
    {#if field.block}
      <p class="field-name">{visible(field.name)}</p>
      <pre>{visibleOutput(field.text)}</pre>
    {:else}
      <p>
        <span class="field-name">{visible(field.name)}</span>
        <code>{visible(field.text)}</code>
      </p>
    {/if}
  {/each}

  {#if result.error}
    <p class="problem">{visible(`${result.error.kind}: ${result.error.message}`)}</p>
    {#if result.error.traceback}
      <pre>{visibleOutput(result.error.traceback)}</pre>
    {/if}
  {/if}
  {#each result.streams as stream (stream.name)}
    <p class="field-name">{stream.name}</p>
    <pre>{visibleOutput(stream.text)}</pre>
  {/each}
  {#if result.notices.length > 0}
    <ul class="notices">
      {#each result.notices as notice, index (index)}
        <li>{visible(notice)}</li>
      {/each}
    </ul>
  {/if}
  {#if result.text !== null}
    <pre>{visibleOutput(result.text)}</pre>
  {/if}
  {#if result.facts.length > 0}
    <p class="muted">{visible(result.facts.join(' · '))}</p>
  {/if}
  {#if result.rest}
    <p class="field-name">other details</p>
    <pre>{visible(result.rest)}</pre>
  {/if}

  {#if step.record}
    <details class="record" bind:open={showRecord}>
      <summary>Full record</summary>
      {#if showRecord}
        <pre>{visible(JSON.stringify(step.record, null, 2))}</pre>
      {/if}
    </details>
  {/if}
</div>
