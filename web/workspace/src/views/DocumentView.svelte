<!-- SPDX-License-Identifier: MIT -->
<!--
  A revision of a living document, read-only: its facts, its warnings, and its text
  rendered with the server's block and math IDs and its front matter's macros (living
  documents, LD-9, LD-26, LD-28). Where the browser's parse and the server's block map
  disagree, the view says so and leaves those parts without IDs.
-->
<script lang="ts">
  import { api } from '../lib/api';
  import type { Mismatch } from '../lib/attach';
  import { describeAuthor } from '../lib/authors';
  import { formatTime } from '../lib/format';
  import { macroSet } from '../lib/macros';
  import Markdown from '../lib/Markdown.svelte';
  import { documentHref, historyHref, projectHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import DocumentEditor from './DocumentEditor.svelte';
  const commentComponent = import('./DocumentComments.svelte');

  let {
    projectId,
    documentId,
    revision,
  }: { projectId: string; documentId: string; revision: number | null } = $props();
  let refresh = $state(0);
  const loading = $derived.by(() => { refresh; return api.document(projectId, documentId, revision); });
  let mismatches: Mismatch[] = $state([]);

  function lines(found: Mismatch): string {
    const [first, end] = found.lines;
    return end - first <= 1 ? `line ${first + 1}` : `lines ${first + 1}–${end}`;
  }
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> / documents /
</nav>

{#await loading}
  <p class="muted">Loading…</p>
{:then found}
  {@const info = found.document}
  {@const shown = found.revision}
  {@const macros = macroSet(found.macros)}
  <!-- A first revision added every block: marking them all would say nothing. -->
  {@const blocks =
    shown.parent === null ? found.blocks.map((block) => ({ ...block, status: 'kept' })) : found.blocks}
  {@const input = { blocks, macros }}
  <h1>{found.title ?? info.title}</h1>
  <dl class="facts">
    <dt>Document</dt>
    <dd>
      <code>{info.name}</code>
      <span class="muted">
        {info.scope === 'project' ? "the project's" : `in session ${info.session_id}'s workbook`},
        {info.document_id}
      </span>
    </dd>
    <dt>Revision</dt>
    <dd>
      {shown.number} of {info.head}
      {#if shown.number > 1}
        · <a href={documentHref(projectId, info.document_id, shown.number - 1)}>previous</a>
      {/if}
      {#if shown.number < info.head}
        · <a href={documentHref(projectId, info.document_id, shown.number + 1)}>next</a>
        · <a href={documentHref(projectId, info.document_id)}>current</a>
      {/if}
      · <a href={historyHref(projectId, info.document_id)}>history</a>
    </dd>
    <dt>Written by</dt>
    <dd>
      {describeAuthor(shown.author)}
      {#if shown.accepted_by}
        <span class="muted">(a suggestion, accepted by {describeAuthor(shown.accepted_by)})</span>
      {/if}
    </dd>
    <dt>When</dt>
    <dd>{formatTime(shown.created_at)}</dd>
    <dt>Reason</dt>
    <dd>{shown.reason}</dd>
    {#if shown.normalized.converted && Object.keys(shown.normalized.converted).length > 0}
      <dt>Delimiters</dt>
      <dd>saved in the dollar form (LD-42)</dd>
    {/if}
  </dl>

  {#key `${info.document_id}:${shown.number}`}
    <DocumentEditor {projectId} {found} refreshed={() => refresh += 1} />
  {/key}

  {#if shown.warnings.length > 0}
    <h2>Warnings</h2>
    <ul class="warnings">
      {#each shown.warnings as warning, index (index)}
        <li>
          <code>{warning.kind}</code>
          {#if warning.block}<span class="muted">in {warning.block}{warning.math ? `, ${warning.math}` : ''}</span>{/if}:
          {warning.message}
        </li>
      {/each}
    </ul>
  {/if}

  {#if macros.problems.length > 0}
    <p class="problem">
      Some of the front matter's macros are not used: {macros.problems.join('; ')}.
    </p>
  {/if}

  {#if !found.matched}
    <p class="problem">
      The server could not parse this revision as its block map was made, with a parser of
      another version, so its formulas are shown without their IDs.
    </p>
  {/if}

  {#if mismatches.length > 0}
    <div class="mismatch" role="note">
      <p>
        The browser reads this revision differently from the server in {mismatches.length}
        {mismatches.length === 1 ? 'place' : 'places'} (LD-9). Those parts are shown without
        their IDs, outlined:
      </p>
      <ul>
        {#each mismatches as mismatch, index (index)}
          <li>{lines(mismatch)}: {mismatch.message}</li>
        {/each}
      </ul>
    </div>
  {/if}

  {#if shown.tombstone}
    <p>The document was deleted in this revision. Its earlier revisions remain.</p>
  {:else}
    <article class="document">
      <Markdown source={found.text} document={input} bind:mismatches />
    </article>
  {/if}
  {#key `${info.document_id}:${shown.number}`}
    {#await commentComponent then component}
      {@const Comments = component.default}
      <Comments {projectId} {found} />
    {:catch error}<ErrorMessage {error} />{/await}
  {/key}
{:catch error}
  <ErrorMessage {error} />
{/await}
