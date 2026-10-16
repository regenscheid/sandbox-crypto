<!-- SPDX-License-Identifier: MIT -->
<!-- One reference to a recorded file where the text writes it (figures, FG-8, FG-9, FG-12,
     FG-17, FG-18): the figure, from the preview origin, with its pinned revision and a
     newer one, and in a document "Move the pin" to the newer; a type shown as a link; or a
     broken reference with its path and why. Past the text's limit it shows its source and
     a button that draws it. Never a blank, and its text is shown as text. -->
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
  // svelte-ignore state_referenced_locally
  let drawing = $state(drawn);
  let moving = $state(false);
  let failed = $state<string | null>(null);
</script>

<span class="figure-reference" role="figure" aria-label={label}>
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
    <span><bdi>{visible(view.path)}</bdi> is shown as a link: its type is not drawn.</span>
  {/if}
  {#if view.shown !== 'broken'}
    <span class="caption muted">
      <bdi>{label}</bdi> · <bdi>{visible(view.path)}</bdi>, {revisionText(view)}
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

<style>
  .figure-reference,
  .caption {
    display: block;
  }
  .figure-reference {
    margin: 0.5rem 0;
  }
</style>
