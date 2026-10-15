<!-- SPDX-License-Identifier: MIT -->
<!-- One artifact at its latest revision (research artifacts, RA-37): only props, so a test
     renders it on the server. A PDF whose bytes were kept links to the viewer. -->
<script lang="ts">
  import type { ArtifactEntry } from '../lib/api-types';
  import { keptText, typeText, whoText } from '../lib/artifactFiles';
  import { formatTime } from '../lib/format';
  import { sizeText, viewable } from '../lib/pdfAnchors';
  import { pdfHref } from '../lib/router';

  let { projectId, entry, viewer }: { projectId: string; entry: ArtifactEntry; viewer: boolean } =
    $props();
  const linked = $derived(!entry.deleted && entry.content_retained && viewable(entry, viewer));
</script>

<tr class:deleted={entry.deleted}>
  <td class="path">
    {#if linked}
      <a href={pdfHref(projectId, entry.artifact_id, entry.revision, entry.sha256)}
        ><bdi>{entry.path}</bdi></a
      >
    {:else}
      <bdi>{entry.path}</bdi>
    {/if}
    {#if keptText(entry)}<br /><small class="muted">{keptText(entry)}</small>{/if}
  </td>
  <td><code>{typeText(entry)}</code></td>
  <td>{entry.revision}</td>
  <td>{entry.deleted ? '' : sizeText(entry.size)}</td>
  <td><bdi>{whoText(entry)}</bdi></td>
  <td>{formatTime(entry.created_at)}</td>
</tr>

<style>
  .path {
    overflow-wrap: anywhere;
  }
  .deleted td {
    text-decoration: line-through;
  }
</style>
