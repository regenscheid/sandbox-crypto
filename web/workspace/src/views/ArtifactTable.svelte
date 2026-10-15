<!-- SPDX-License-Identifier: MIT -->
<!-- One group of the project's artifacts (research artifacts, RA-37): results, or every
     other recorded file, at their latest revision, each PDF with a link to the viewer. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { ArtifactEntry } from '../lib/api-types';
  import { type ArtifactFilter, type ArtifactGroup, keptText, whoText } from '../lib/artifactFiles';
  import { formatTime } from '../lib/format';
  import { emptyList, PagedList } from '../lib/pagedList';
  import { sizeText, viewable } from '../lib/pdfAnchors';
  import { pdfHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    group,
    filter,
    empty,
  }: { projectId: string; group: ArtifactGroup; filter: ArtifactFilter; empty: string } = $props();
  let viewer = $state(false);
  const artifacts = $state(emptyList<ArtifactEntry, string>());
  const list = new PagedList(artifacts, async (after) => {
    const page = await api.artifacts(projectId, after, group, filter);
    return {
      items: page.artifacts,
      next: page.next_after,
      shown: () => {
        viewer = page.viewer;
      },
    };
  });
  $effect(() => {
    // A new filter reads the list again.
    void [filter.session, filter.run];
    void list.reload();
  });
</script>

{#if artifacts.items.length === 0 && !artifacts.loading && !artifacts.error}
  <p class="muted">{empty}</p>
{:else if artifacts.items.length > 0}
  <table>
    <thead>
      <tr>
        <th>Path</th>
        <th>Type</th>
        <th>Revision</th>
        <th>Size</th>
        <th>By</th>
        <th>When</th>
      </tr>
    </thead>
    <tbody>
      {#each artifacts.items as entry (entry.artifact_id)}
        <tr class:deleted={entry.deleted}>
          <td>
            {#if !entry.deleted && entry.content_retained && viewable(entry, viewer)}
              <a href={pdfHref(projectId, entry.artifact_id, entry.revision, entry.sha256)}
                >{entry.path}</a
              >
            {:else}
              {entry.path}
            {/if}
            {#if keptText(entry)}<br /><small class="muted">{keptText(entry)}</small>{/if}
          </td>
          <td><code>{entry.media_type}</code></td>
          <td>{entry.revision}</td>
          <td>{entry.deleted ? '' : sizeText(entry.size)}</td>
          <td>{whoText(entry)}</td>
          <td>{formatTime(entry.created_at)}</td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}
{#if artifacts.loading}<p class="muted">Loading…</p>{/if}
{#if artifacts.error}<ErrorMessage error={artifacts.error} />{/if}
{#if artifacts.next}
  <button type="button" disabled={artifacts.loading} onclick={() => list.more()}>More</button>
{/if}

<style>
  .deleted td {
    text-decoration: line-through;
  }
</style>
