<!-- SPDX-License-Identifier: MIT -->
<!-- The project's published artifacts (docs/specs/pdf-viewer.md, PV-4), each PDF with a
     link that opens it in the viewer. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { ArtifactEntry } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { emptyList, PagedList } from '../lib/pagedList';
  import { sizeText, viewable } from '../lib/pdfAnchors';
  import { pdfHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId }: { projectId: string } = $props();
  let viewer = $state(false);
  const artifacts = $state(emptyList<ArtifactEntry, string>());
  const list = new PagedList(artifacts, async (after) => {
    const page = await api.artifacts(projectId, after);
    return {
      items: page.artifacts,
      next: page.next_after,
      shown: () => {
        viewer = page.viewer;
      },
    };
  });
  void list.reload();
</script>

<h2>Artifacts</h2>
{#if artifacts.items.length === 0 && !artifacts.loading && !artifacts.error}
  <p>
    No published artifacts yet. An agent publishes a file from its workspace with <code
      >publish</code
    >.
  </p>
{:else if artifacts.items.length > 0}
  <table>
    <thead>
      <tr>
        <th>Path</th>
        <th>Type</th>
        <th>Revision</th>
        <th>Size</th>
        <th>Published</th>
      </tr>
    </thead>
    <tbody>
      {#each artifacts.items as entry (entry.artifact_id)}
        <tr>
          <td>
            {#if viewable(entry, viewer)}
              <a href={pdfHref(projectId, entry.artifact_id, entry.revision, entry.sha256)}
                >{entry.path}</a
              >
            {:else}
              {entry.path}
            {/if}
          </td>
          <td><code>{entry.media_type}</code></td>
          <td>{entry.revision}</td>
          <td>{sizeText(entry.size)}</td>
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
