<!-- SPDX-License-Identifier: MIT -->
<!-- One step of a finding's history: its decision, its reason rendered, and the reason
     exactly as recorded, which an agent may have written and the user decides on
     (threat model, A3), with a banner when the rendering hides anything. Only props,
     so a test renders it on the server. -->
<script lang="ts">
  import { transitionText } from '../lib/findingView';
  import Markdown from '../lib/Markdown.svelte';
  import { macroSet } from '../lib/macros';
  import type { Transition } from '../lib/reviews';
  import { visible } from '../lib/text';
  import ApprovalText from './ApprovalText.svelte';
  import HiddenBanner from './HiddenBanner.svelte';
  import ResponseBody from './ResponseBody.svelte';

  let { projectId, sessionId, entry }: { projectId: string; sessionId: string; entry: Transition } =
    $props();
</script>

<div class="transition">
  <p>{transitionText(entry)}</p>
  {#each entry.macro_context?.problems ?? [] as problem}<p role="status">
      {problem}
    </p>{/each}<Markdown
    source={entry.reason}
    macros={macroSet(
      entry.macro_context?.macros ?? {},
      `${projectId}:${entry.macro_context?.key ?? 'unknown-history'}`,
    )}
  />
  <HiddenBanner text={entry.reason} links />
  <details>
    <summary>The reason, exactly as recorded</summary>
    <ApprovalText text={entry.reason} label="The reason, exactly" />
  </details>
  <details>
    <summary>Decision evidence and attribution</summary>
    <pre>{visible(JSON.stringify(entry.payload, null, 2))}</pre>
  </details>
  {#if entry.response_body}<ResponseBody
      {projectId}
      {sessionId}
      source={entry.response_body}
    />{/if}
</div>

<style>
  .transition {
    border-top: 1px solid var(--border, #9996);
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 18rem;
    overflow: auto;
  }
</style>
