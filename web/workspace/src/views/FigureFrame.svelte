<!-- SPDX-License-Identifier: MIT -->
<!-- One pinned figure in a frame on the preview origin (docs/specs/figures.md, FG-12, FG-14,
     FG-17, FG-18): an SVG in the figure frame, which is posted its capability and draws it
     as an image; an HTML file as the page of a frame without scripts, at a fixed height
     with its own scrolling; a PDF in the viewer's figure mode. The frame is made when the
     figure comes near the view, at most eight draw at once, and one not drawn in five
     seconds is removed. Only a status in the protocol's exact shape, from this frame's
     window and the preview origin, is taken. A figure that is not drawn shows its path
     and the reason: never a blank. -->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api, ApiError } from '../lib/api';
  import type { FigureLink } from '../lib/api-types';
  import {
    drawing,
    DRAW_MILLISECONDS,
    HTML_FRAME_HEIGHT,
    imageSource,
    MAX_FRAME_HEIGHT,
  } from '../lib/figures';
  import { CODE_TEXT, figureStatus } from '../figures/protocol';

  let {
    projectId,
    artifact,
    revision,
    sha256,
    alt,
    limit = DRAW_MILLISECONDS,
  }: {
    projectId: string;
    artifact: string;
    revision: number;
    sha256: string;
    alt: string;
    limit?: number;
  } = $props();
  let near = $state<HTMLElement>();
  let frame = $state<HTMLIFrameElement>();
  let link = $state<FigureLink | null>(null);
  let origin: string | null = null;
  let problem = $state<string | null>(null);
  let drawn = $state(false);
  let height = $state(0);
  let release: (() => void) | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let observer: IntersectionObserver | undefined;

  function end(reason: string | null): void {
    clearTimeout(timer);
    release?.();
    release = null;
    if (reason !== null) {
      problem = reason;
      link = null; // the frame is removed
    } else drawn = true;
  }

  async function start(): Promise<void> {
    release = await drawing.acquire();
    try {
      const found = await api.figureLink(projectId, artifact, revision, sha256);
      origin = imageSource(found.url) === null ? null : new URL(found.url).origin;
      if (origin === null || found.kind === 'image') return end('the server gave no usable frame');
      timer = setTimeout(() => end(`not drawn within ${limit / 1000} seconds`), limit);
      link = found;
    } catch (caught) {
      end(caught instanceof ApiError ? caught.message : 'the server could not be reached');
    }
  }

  /** The frame's page loaded: an HTML figure is drawn; the figure frame is sent its one
   * request, to the preview origin only. */
  function loaded(): void {
    if (!link || !frame?.contentWindow || origin === null) return;
    if (link.kind === 'html') return end(null);
    if (link.kind === 'svg' && link.capability) {
      const request = { type: 'figure', kind: 'svg', source: '', capabilities: [link.capability] };
      frame.contentWindow.postMessage(request, origin);
    }
  }

  function heard(event: MessageEvent): void {
    if (!link || drawn || !frame || event.source !== frame.contentWindow) return;
    if (event.origin !== origin) return;
    const status = figureStatus(event.data);
    if (status === null) return;
    // The frame's words never reach the page: only a code, in the workspace's words.
    if (status.status === 'error') return end(CODE_TEXT[status.code || 'unreadable']);
    height = Math.min(MAX_FRAME_HEIGHT, Math.max(20, status.height));
    end(null);
  }

  onMount(() => {
    window.addEventListener('message', heard);
    if (typeof IntersectionObserver === 'undefined' || !near) {
      void start();
      return;
    }
    observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          observer?.disconnect();
          void start();
        }
      },
      { rootMargin: '200px' },
    );
    observer.observe(near);
  });
  onDestroy(() => {
    observer?.disconnect();
    if (typeof window !== 'undefined') window.removeEventListener('message', heard);
    clearTimeout(timer);
    release?.();
  });
</script>

<div class="figure" bind:this={near}>
  {#if problem !== null}
    <p class="problem">Not drawn: <bdi>{alt}</bdi> ({problem}).</p>
  {:else if link?.kind === 'html'}
    <iframe
      bind:this={frame}
      src={link.url}
      sandbox=""
      referrerpolicy="no-referrer"
      title={alt}
      style:height="{HTML_FRAME_HEIGHT}px"
      onload={loaded}
    ></iframe>
  {:else if link}
    <iframe
      bind:this={frame}
      src={link.url}
      sandbox="allow-scripts allow-same-origin"
      referrerpolicy="no-referrer"
      title={alt}
      class:pending={!drawn}
      style:height="{drawn ? height : 0}px"
      onload={loaded}
    ></iframe>
    {#if !drawn}<span class="muted">Drawing…</span>{/if}
  {:else}
    <span class="muted">Waiting to draw…</span>
  {/if}
</div>

<style>
  iframe {
    display: block;
    width: 100%;
    border: 1px solid var(--border, #ccc);
  }
  iframe.pending {
    visibility: hidden;
  }
</style>
