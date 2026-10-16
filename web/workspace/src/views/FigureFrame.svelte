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
  import { CODE_TEXT, figureStatus, type FigureRequest } from '../figures/protocol';
  import type { ChartData } from '../lib/figureRefs';

  let {
    projectId,
    artifact = '',
    revision = 0,
    sha256 = '',
    chart = null,
    alt,
    limit = DRAW_MILLISECONDS,
  }: {
    projectId: string;
    artifact?: string;
    revision?: number;
    sha256?: string;
    /** A chart's text and the data files it pins, in the order it names them (FG-15). */
    chart?: { source: string; data: ChartData[] } | null;
    alt: string;
    limit?: number;
  } = $props();
  let near = $state<HTMLElement>();
  let frame = $state<HTMLIFrameElement>();
  let link = $state<{ kind: FigureLink['kind'] | 'chart'; url: string } | null>(null);
  /** The one request the figure frame is sent, once it has loaded. */
  let request: FigureRequest | null = null;
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
      let found: { kind: FigureLink['kind'] | 'chart'; url: string };
      if (chart !== null) {
        const minted = await api.chartLink(projectId, chart.data);
        found = { kind: 'chart', url: minted.url };
        const paths = chart.data.map((file) => file.path);
        request = {
          type: 'figure',
          kind: 'vega-lite',
          source: chart.source,
          paths,
          capabilities: minted.capabilities,
        };
      } else {
        const minted = await api.figureLink(projectId, artifact, revision, sha256);
        found = minted;
        if (minted.kind === 'svg' && minted.capability)
          request = {
            type: 'figure',
            kind: 'svg',
            source: '',
            paths: [''],
            capabilities: [minted.capability],
          };
      }
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
    if (request !== null) frame.contentWindow.postMessage(request, origin);
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
