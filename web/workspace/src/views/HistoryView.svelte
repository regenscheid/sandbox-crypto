<!-- SPDX-License-Identifier: MIT -->
<!--
  Who changed what in a living document (LD-11): each revision with its author, reason
  and the blocks it changed; for each block, the revisions that changed it; and the
  patches agents submitted, with their states.
-->
<script lang="ts">
  import { api, type DocumentHistory } from '../lib/api';
  import { describeAuthor, describeChanges } from '../lib/authors';
  import { Detail, noDetail } from '../lib/detail';
  import { newestFirst, reviewable } from '../lib/documentHistory';
  import { formatTime } from '../lib/format';
  import { visible } from '../lib/text';
  import { documentHref, projectHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import PatchReview from './PatchReview.svelte';

  let { projectId, documentId }: { projectId: string; documentId: string } = $props();
  const shown = $state(noDetail<string, DocumentHistory>());
  // Keyed by the document alone: a document ID is unique in the whole store (the documents
  // table's key), and the read names the project as the view's properties do now.
  const histories = new Detail(shown, (document) => api.history(projectId, document));
  $effect(() => {
    void histories.show(documentId);
  });
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> / documents /
</nav>

{#if shown.error}
  <ErrorMessage error={shown.error} />
{:else if !shown.value}
  <p class="muted">Loading…</p>
{:else}
  {@const history = shown.value}
  {@const info = history.document}
  <h1>History of {info.title}</h1>
  <p>
    <code>{info.name}</code>, revision {info.head}:
    <a href={documentHref(projectId, info.document_id)}>the current text</a>.
  </p>

  <h2>Revisions</h2>
  <table>
    <thead>
      <tr>
        <th>Revision</th>
        <th>When</th>
        <th>Written by</th>
        <th>Reason</th>
        <th>Blocks</th>
      </tr>
    </thead>
    <tbody>
      {#each newestFirst(history.revisions) as revision (revision.number)}
        <tr>
          <td
            ><a href={documentHref(projectId, info.document_id, revision.number)}
              >{revision.number}</a
            ></td
          >
          <td>{formatTime(revision.created_at)}</td>
          <td>
            {describeAuthor(revision.author)}
            {#if revision.accepted_by}
              <span class="muted">accepted by {describeAuthor(revision.accepted_by)}</span>
            {/if}
          </td>
          <td>{visible(revision.reason)}</td>
          <td>
            {revision.tombstone ? 'deleted the document' : describeChanges(revision)}
            {#if revision.warnings > 0}
              <span class="muted"
                >({revision.warnings} {revision.warnings === 1 ? 'warning' : 'warnings'})</span
              >
            {/if}
          </td>
        </tr>
      {/each}
    </tbody>
  </table>

  <h2>Blocks</h2>
  <details class="blocks">
    <summary>
      The revisions that changed each of the {Object.keys(history.blocks).length} blocks
    </summary>
    <table>
      <thead>
        <tr>
          <th>Block</th>
          <th>Revisions</th>
        </tr>
      </thead>
      <tbody>
        {#each Object.entries(history.blocks) as [block, changes] (block)}
          <tr>
            <td><code>{block}</code></td>
            <td>
              {#each changes as change, index (index)}
                {index > 0 ? '; ' : ''}{change.status} in
                <a href={documentHref(projectId, info.document_id, change.revision)}
                  >{change.revision}</a
                >
                by {describeAuthor(change.author)}
              {/each}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </details>

  <!-- A new head (an applied patch, or any other change) closes every open preview. -->
  {#key history.document.head}
    {#if history.patches.length > 0}
      <h2>Patches</h2>
      <table>
        <thead>
          <tr>
            <th>Patch</th>
            <th>By</th>
            <th>Base</th>
            <th>State</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {#each newestFirst(history.patches) as patch (patch.patch_id)}
            <tr>
              <td><code>{patch.patch_id}</code></td>
              <td>{describeAuthor(patch.author)}</td>
              <td>{patch.base}</td>
              <td>
                {patch.state}
                {#if patch.revision}
                  <span class="muted">
                    in <a href={documentHref(projectId, info.document_id, patch.revision)}
                      >{patch.revision}</a
                    >
                  </span>
                {/if}
              </td>
              <td
                >{visible(patch.reason)}
                {#if reviewable(patch)}
                  <PatchReview
                    {projectId}
                    documentId={info.document_id}
                    patchId={patch.patch_id}
                    suggested={patch.state === 'suggested'}
                    refreshed={() => void histories.reload()}
                  />
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  {/key}
{/if}
