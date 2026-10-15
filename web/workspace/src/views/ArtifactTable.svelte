<!-- SPDX-License-Identifier: MIT -->
<!-- One group of the project's artifacts (research artifacts, RA-37): results, or every
     other recorded file, the most recently changed first, a page at a time. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { ArtifactEntry } from '../lib/api-types';
  import { type ArtifactFilter, type ArtifactGroup } from '../lib/artifactFiles';
  import { emptyList, PagedList } from '../lib/pagedList';
  import ArtifactRow from './ArtifactRow.svelte';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    group,
    filter,
    empty,
  }: { projectId: string; group: ArtifactGroup; filter: ArtifactFilter; empty: string } = $props();
  let viewer = $state(false);
  const artifacts = $state(emptyList<ArtifactEntry, number>());
  const list = new PagedList(artifacts, async (before) => {
    const page = await api.artifacts(projectId, before, group, filter);
    return {
      items: page.artifacts,
      next: page.next_before,
      shown: () => {
        viewer = page.viewer;
      },
    };
  });
  $effect(() => {
    // A new filter names another list: the one shown goes at once.
    void [filter.session, filter.run, filter.contains];
    void list.restart();
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
        <ArtifactRow {projectId} {entry} {viewer} />
      {/each}
    </tbody>
  </table>
{/if}
{#if artifacts.loading}<p class="muted">Loading…</p>{/if}
{#if artifacts.error}<ErrorMessage error={artifacts.error} />{/if}
{#if artifacts.next !== null}
  <button type="button" disabled={artifacts.loading} onclick={() => list.more()}>More</button>
{/if}

<style>
  table {
    table-layout: fixed;
    width: 100%;
  }
</style>
