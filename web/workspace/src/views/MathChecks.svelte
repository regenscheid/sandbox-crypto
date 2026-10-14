<!-- SPDX-License-Identifier: MIT -->
<!-- The session's mechanical math checks, what waits for them, and one check's evidence. -->
<script lang="ts">
  import type { ReviewBrowser, ReviewPanel } from '../lib/reviewBrowser';
  import { mathCheckText, queueText } from '../lib/reviewText';
  import ErrorMessage from './ErrorMessage.svelte';
  let { panel, browser }: { panel: ReviewPanel; browser: ReviewBrowser } = $props();
</script>

<h3>Mechanical math checks</h3>
<p class="muted">
  Mechanical checks use the operator-configured renderer. Unavailable, queued and quota-held work
  cannot establish a fix. Checks with no eligible author remain visible here.
</p>
{#if panel.queue.length}
  <h4>Waiting or held</h4>
  <ul>
    {#each panel.queue as item, index (index)}<li>{queueText(item)}</li>{/each}
  </ul>
  {#if panel.queueMore}<p>Additional sources remain queued or held.</p>{/if}
{/if}
{#if !panel.checks.items.length}<p>No mechanical math checks recorded.</p>{/if}
<ul>
  {#each panel.checks.items as check (check.check_id)}<li>
      <button onclick={() => browser.readMath(check.check_id)}>Read evidence</button>
      {mathCheckText(check)}
      {#if check.document}<span class="muted">in <code>{check.document}</code></span>{/if}
      {#if check.macro_context_status}<p class="muted">
          Project macro revision {check.project_macro_revision}; context {check.macro_context_status}.
        </p>{/if}
      {#if check.comment}<p class="muted">
          Original entry <code>{check.comment.entry_id}</code>; document macro revision {check
            .comment.macro_revision}.
        </p>{/if}
      {#if check.response}<p class="muted">
          Finding <code>{check.response.finding}</code>; original response {check.response
            .response_revision}; pinned source macro context.
        </p>{/if}
    </li>{/each}
</ul>
{#if panel.checks.next}<button onclick={() => browser.checks.more()}>Load more math checks</button
  >{/if}
{#if panel.checks.error}<ErrorMessage error={panel.checks.error} />{/if}
{#if panel.math.items.length}<h4>Mechanical evidence and author provenance</h4>
  <pre aria-label="Document math check">{panel.math.items.join('\n')}</pre>
  {#if panel.math.next !== null}<button onclick={() => browser.math.more()}
      >Read next math evidence page</button
    >{/if}{/if}
{#if panel.math.error}<ErrorMessage error={panel.math.error} />{/if}

<style>
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 22rem;
    overflow: auto;
  }
</style>
