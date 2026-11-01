<!-- SPDX-License-Identifier: MIT -->
<!-- One reference to a recorded file where the text writes it (figures, FG-8, FG-9, FG-12,
     FG-17, FG-18): the figure, from the preview origin, with its pinned revision and a
     newer one, and in a document "Move the pin" to the newer; another type as its file to
     download; or a broken reference with its path and why. Past the text's limit it shows
     its source and a button that draws it. Never a blank, and its text is shown as text. -->
<script lang="ts">
  import { pinView, revisionText, type Pin } from '../lib/figureRefs';
  import { visible } from '../lib/text';
  import FigureFrame from './FigureFrame.svelte';
  import FigureImage from './FigureImage.svelte';

  let {
    projectId,
    pin,
    alt,
    drawn = true,
    repin = null,
  }: {
    projectId: string;
    pin: Pin;
    alt: string;
    drawn?: boolean;
    repin?: ((block: string, path: string, revision: number) => Promise<void>) | null;
  } = $props();
  const view = $derived(pinView(pin));
  const label = $derived(visible(alt || view.path));
  const contentHref = $derived(
    `/api/v0/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(view.artifact)}` +
      `/revisions/${view.revision}/content?sha256=${encodeURIComponent(view.sha256)}`,
  );
  // svelte-ignore state_referenced_locally
  let drawing = $state(drawn);
  let moving = $state(false);
  let failed = $state<string | null>(null);
  /** A file that is not drawn reads as a link in its sentence, not a block of its own. */
  const inline = $derived(view.shown === 'link' && drawing);
</script>

<!-- A type not drawn in the text: the file, at its pinned revision, to download. -->
{#snippet fileLink()}<a
    class="file-link"
    href={contentHref}
    download={view.path.split('/').pop()}
    title={`Download ${visible(view.path)}, ${revisionText(view)}`}
    ><svg
      width="13"
      height="13"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      stroke-width="1.5"
      aria-hidden="true"><path d="M4 1.5h5.5L13 5v9.5H4z M9.5 1.5V5H13"></path></svg
    ><bdi>{label}</bdi></a
  >{/snippet}

<!-- A plain file link sits in its sentence with nothing around it: no space before what follows. -->
{#if inline && !view.latest && !failed}<span
    class="figure-reference inline"
    role="figure"
    aria-label={label}>{@render fileLink()}</span
  >{:else}
  <span class="figure-reference" class:inline role="figure" aria-label={label}>
    {#if view.shown === 'broken'}
      <span class="problem">Not drawn: <bdi>{visible(view.path)}</bdi> ({view.reason}).</span>
    {:else if !drawing}
      <code>![{label}]({visible(view.path)})</code>
      <button type="button" onclick={() => (drawing = true)}>Draw this figure</button>
    {:else if view.shown === 'image'}
      <FigureImage
        {projectId}
        artifact={view.artifact}
        revision={view.revision}
        sha256={view.sha256}
        alt={label}
      />
    {:else if view.shown === 'frame'}
      <FigureFrame
        {projectId}
        artifact={view.artifact}
        revision={view.revision}
        sha256={view.sha256}
        alt={label}
      />
    {:else}
      {@render fileLink()}
    {/if}
    <!-- A link's revision is in its title; it says more only when a newer one exists. -->
    {#if view.shown !== 'broken' && (!inline || view.latest || failed)}
      <span class="caption muted">
        {#if inline}{view.latest ? revisionText(view) : ''}{:else}<bdi>{label}</bdi> ·
          <bdi>{visible(view.path)}</bdi>{`, ${revisionText(view)}`}{/if}
        {#if repin && view.latest && view.block}
          <button
            type="button"
            disabled={moving}
            onclick={async () => {
              moving = true;
              failed = null;
              try {
                await repin(view.block!, view.path, view.latest!);
              } catch (caught) {
                failed = caught instanceof Error ? caught.message : 'the pin was not moved';
              } finally {
                moving = false;
              }
            }}>Move the pin to r{view.latest}</button
          >
        {/if}
        {#if failed}<span class="problem">The pin was not moved: {failed}</span>{/if}
      </span>
    {/if}
  </span>
{/if}

<style>
  .figure-reference,
  .caption {
    display: block;
  }
  .figure-reference {
    margin: 0.5rem 0;
  }
  .figure-reference.inline,
  .inline .caption {
    display: inline;
    margin: 0;
  }
  .inline .caption {
    margin-left: 4px;
    font-size: 12px;
  }
  .file-link svg {
    vertical-align: -2px;
    margin-right: 3px;
  }
</style>
