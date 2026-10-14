<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { reviews, type ResponseBody } from '../lib/reviews';
  import { Preview } from '../lib/preview';
  import Markdown from '../lib/Markdown.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  import { macroSet } from '../lib/macros';
  let {
    projectId,
    sessionId,
    source,
  }: { projectId: string; sessionId: string; source: ResponseBody } = $props();
  let viewed = $state<ResponseBody | null>(null),
    error = $state<unknown>(null);
  const preview = new Preview();
  $effect(() => {
    source.sha256;
    projectId;
    sessionId;
    preview.invalidate();
    viewed = null;
    error = null;
  });
  const body = $derived(viewed ?? source);
  const macros = $derived(
    macroSet(
      body.macro_context?.macros ?? {},
      `${projectId}:${body.macro_context?.key ?? 'unknown-response'}`,
    ),
  );
  async function earlier() {
    const current = preview.begin(`${projectId}/${sessionId}/${source.source}`);
    const revision = body.revision - 1;
    try {
      const result = await reviews.responseBody(
        projectId,
        sessionId,
        source.finding,
        source.response_revision,
        revision,
      );
      if (current()) viewed = result;
    } catch (caught) {
      if (current()) error = caught;
    }
  }
</script>

<section aria-label="Explanation source">
  <p>
    MAIN explanation source · original decision revision {source.response_revision} · body revision {body.revision}.
  </p>
  <p class="muted">
    Original author {String(body.original_author.run_id ?? '')}; correcting author {String(
      body.author?.run_id ?? 'unknown',
    )}. Ordinary Markdown with its pinned source context and project settings.
  </p>
  {#each body.macro_context?.problems ?? [] as problem}<p role="status">
      {problem} Display uses a diagnostic context.
    </p>{/each}
  <Markdown source={body.body} {macros} />
  {#if body.held}<p role="status">{body.held}</p>{/if}
  <details>
    <summary>Exact explanation source and attribution</summary>
    <pre>{JSON.stringify(body, null, 2)}</pre>
  </details>
  <button disabled={body.revision <= 1} onclick={earlier}>Read previous explanation body</button>
  {#if viewed}<button
      onclick={() => {
        preview.invalidate();
        viewed = null;
      }}>Show current explanation body</button
    >{/if}
  <p class="muted">
    Source corrections preserve the original decision and evidence above. A math check resolves only
    its math finding.
  </p>
  {#if error}<ErrorMessage {error} />{/if}
</section>

<style>
  section {
    border-left: 2px solid var(--border, #9996);
    margin: 0.7rem 0;
    padding-left: 0.7rem;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 18rem;
    overflow: auto;
  }
</style>
