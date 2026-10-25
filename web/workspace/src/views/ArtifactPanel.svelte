<!-- SPDX-License-Identifier: MIT -->
<!--
  The session's Artifacts panel beside the chat (B10, item 6): the artifact chosen from a
  strip under the conversation's steps, in the file viewer with its revisions; with none
  chosen, the session's artifacts, the most recently changed first, a page at a time, each
  opening in the viewer. The choice is kept with the session's other panel state, by the
  artifact and its revision, and read again from its revisions after a reload.
-->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, ARTIFACT_LISTING_MAX_BYTES } from '../lib/api';
  import type { ArtifactEntry, ArtifactListing } from '../lib/api-types';
  import { kindText, splitPath } from '../lib/artifactGrid';
  import type { ArtifactChoice } from '../lib/artifactStrip';
  import { get } from '../lib/http';
  import { emptyList, PagedList } from '../lib/pagedList';
  import { sizeText } from '../lib/pdfAnchors';
  import { visible } from '../lib/text';
  import ArtifactViewer from './ArtifactViewer.svelte';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    sessionId,
    choice,
    choose,
  }: {
    projectId: string;
    sessionId: string;
    choice: ArtifactChoice | null;
    /** Show another artifact or revision, or with null the session's list. */
    choose: (entry: ArtifactEntry | null) => void;
  } = $props();

  let viewer = $state(false);
  const latest = $state(emptyList<ArtifactEntry, number>());
  const list = new PagedList(latest, async (before) => {
    const query = new URLSearchParams({ session: sessionId });
    if (before !== null) query.set('before', String(before));
    const page = await get<ArtifactListing>(
      `/projects/${encodeURIComponent(projectId)}/artifacts?${query}`,
      { maxBytes: ARTIFACT_LISTING_MAX_BYTES },
    );
    return { items: page.artifacts, next: page.next_before, shown: () => (viewer = page.viewer) };
  });
  void untrack(() => list.reload());

  /** The most pages of revisions read to find a kept choice's entry. */
  const REVISION_PAGES = 5;
  let resolving = $state<unknown>(null);
  let missing = $state<string | null>(null);
  const wanted = $derived(choice && !choice.entry ? `${choice.artifact}:${choice.revision}` : '');
  $effect(() => {
    if (!wanted) return;
    const { artifact, revision } = untrack(() => choice!);
    resolving = null;
    missing = null;
    let cancelled = false;
    void (async () => {
      try {
        let before: number | null = null;
        for (let page = 0; page < REVISION_PAGES; page++) {
          const found = await api.artifactRevisions(projectId, artifact, before);
          const entry = found.revisions.find((item) => item.revision === revision);
          if (cancelled) return;
          if (entry) return choose(entry);
          before = found.next_before;
          if (before === null) break;
        }
        if (!cancelled) missing = `Revision ${revision} of this artifact is not listed.`;
      } catch (caught) {
        if (!cancelled) resolving = caught;
      }
    })();
    return () => {
      cancelled = true;
    };
  });
</script>

<h2>Artifacts</h2>
{#if choice?.entry}
  {@const entry = choice.entry}
  <ArtifactViewer
    {projectId}
    {entry}
    {viewer}
    close={() => choose(null)}
    choose={(picked) => choose(picked)}
  />
{:else if choice}
  <p class="muted" role="status">
    {missing ??
      (resolving ? '' : `Reading revision ${choice.revision} of ${visible(choice.artifact)}…`)}
  </p>
  {#if resolving}<ErrorMessage error={resolving} />{/if}
  {#if resolving || missing}
    <button type="button" class="link-button" onclick={() => choose(null)}
      >The session's artifacts</button
    >{/if}
{:else}
  <p class="muted">
    The session's files, the most recently changed first. Choose one, or a file under the
    conversation's steps, to open it here with its revisions.
  </p>
  {#if !latest.items.length && !latest.loading && latest.error === null}<p>
      No artifacts recorded in this session yet.
    </p>{/if}
  <ul class="artifact-latest">
    {#each latest.items as entry (entry.artifact_id)}
      {@const parts = splitPath(entry.path)}
      <li>
        <button
          type="button"
          class="link-button"
          title={visible(entry.path)}
          onclick={() => choose(entry)}><bdi>{visible(parts.name)}</bdi></button
        >
        <small class="muted"
          ><bdi>{visible(parts.folder) || '/'}</bdi> · {kindText(entry)} · revision {entry.revision}{#if !entry.deleted}&nbsp;·
            {sizeText(entry.size)}{/if}</small
        >
      </li>
    {/each}
  </ul>
  {#if latest.next !== null}<button
      type="button"
      disabled={latest.loading}
      onclick={() => list.more()}>Load more artifacts</button
    >{/if}
  <button type="button" disabled={latest.loading} onclick={() => list.reload()}>Read again</button>
  {#if latest.error}<ErrorMessage error={latest.error} />{/if}
{/if}

<style>
  .artifact-latest {
    list-style: none;
    margin: 8px 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .artifact-latest li {
    display: flex;
    flex-direction: column;
  }
</style>
