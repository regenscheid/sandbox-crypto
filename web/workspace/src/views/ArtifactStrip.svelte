<!-- SPDX-License-Identifier: MIT -->
<!--
  The files a work group's calls recorded or published (B10, item 6), from their results'
  glosses: each chip names the file, its kind and size, with a thumbnail for an image the
  preview origin serves, and opens it in the session's Artifacts panel. A path the run's
  artifact list does not have yet shows as text, "not yet listed".
-->
<script lang="ts">
  import type { ArtifactEntry } from '../lib/api-types';
  import { stripChips, type RunListing, type StripKind } from '../lib/artifactStrip';
  import type { StepCall } from '../lib/conversation';
  import { filesHref } from '../lib/router';
  import FigureImage from './FigureImage.svelte';
  import Icon, { type IconName } from './Icon.svelte';

  let {
    projectId,
    sessionId,
    run,
    calls,
    listing = undefined,
    show = null,
  }: {
    projectId: string;
    sessionId: string;
    run: string;
    calls: readonly StepCall[];
    /** The run's artifact list as read so far. */
    listing?: RunListing | undefined;
    /** Open an artifact in the session's Artifacts panel. */
    show?: ((entry: ArtifactEntry) => void) | null;
  } = $props();
  const strip = $derived(stripChips(calls, listing?.entries ?? null, listing?.viewer ?? false));
  const ICONS: Record<StripKind, IconName> = {
    table: 'overview',
    figure: 'artifact',
    pdf: 'document',
    text: 'document',
    other: 'artifact',
  };
</script>

{#if strip.chips.length}
  <ul class="artifact-strip" aria-label="Files these steps recorded or published">
    {#each strip.chips as chip (chip.path)}
      <li>
        {#snippet face()}
          {#if chip.thumbnail && chip.entry}<span class="artifact-thumb"
              ><FigureImage
                {projectId}
                artifact={chip.entry.artifact_id}
                revision={chip.entry.revision}
                sha256={chip.entry.sha256}
                alt={chip.path}
              /></span
            >{:else}<Icon name={ICONS[chip.kind]} size={15} />{/if}
          <span class="artifact-name"><bdi>{chip.name}</bdi></span>
          <small
            >{chip.kindText}{#if chip.size}&nbsp;· {chip.size}{/if}</small
          >
        {/snippet}
        {#if chip.entry && show}
          {@const entry = chip.entry}
          <button
            type="button"
            class="artifact-chip"
            title={chip.path}
            data-kind={chip.kind}
            onclick={() => show(entry)}>{@render face()}</button
          >
        {:else}
          <span class="artifact-chip unlisted" title={chip.path} data-kind={chip.kind}
            >{@render face()}{#if !chip.entry}<small class="not-listed">not yet listed</small
              >{/if}</span
          >
        {/if}
      </li>
    {/each}
    {#if strip.more}<li>
        <a class="artifact-more" href={filesHref(projectId, sessionId, run)}
          >{strip.more} more {strip.more === 1 ? 'file' : 'files'}</a
        >
      </li>{/if}
  </ul>
  {#if listing?.error}<p class="muted artifact-strip-error">
      The run's files could not be listed: {listing.error}
    </p>{/if}
{/if}

<style>
  .artifact-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    list-style: none;
    margin: 6px 0 0;
    padding: 0;
  }
  .artifact-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    max-width: 22rem;
    padding: 4px 8px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    color: var(--body-text);
    font: inherit;
    font-size: 12px;
    text-align: left;
  }
  button.artifact-chip {
    cursor: pointer;
  }
  button.artifact-chip:hover {
    background: var(--subtle);
  }
  .artifact-chip.unlisted {
    border-style: dashed;
  }
  .artifact-name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 500;
  }
  .artifact-chip small {
    color: var(--muted);
    white-space: nowrap;
  }
  .artifact-thumb {
    display: inline-block;
    width: 2.5rem;
    height: 2rem;
    overflow: hidden;
  }
  .artifact-thumb :global(img) {
    max-width: 2.5rem;
    max-height: 2rem;
  }
  .artifact-more {
    font-size: 12px;
  }
  .artifact-strip-error {
    font-size: 12px;
    margin: 4px 0 0;
  }
</style>
