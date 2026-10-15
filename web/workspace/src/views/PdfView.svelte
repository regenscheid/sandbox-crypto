<!-- SPDX-License-Identifier: MIT -->
<!-- A published PDF in the viewer, which runs on the preview origin in a sandboxed frame
     (docs/specs/pdf-viewer.md, PV-2, PV-6), and the last anchor it sent. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { ViewerLink } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import {
    anchorText,
    atPage,
    receivedAnchor,
    viewerOrigin,
    type PdfAnchor,
  } from '../lib/pdfAnchors';
  import { projectHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    artifactId,
    revision,
    sha256,
    page = null,
  }: {
    projectId: string;
    artifactId: string;
    revision: number;
    sha256: string;
    /** The page to open the PDF at, such as a quote's; the first when none. */
    page?: number | null;
  } = $props();
  let link = $state<ViewerLink | null>(null);
  let error = $state<unknown>(null);
  let frame = $state<HTMLIFrameElement | null>(null);
  let anchor = $state<PdfAnchor | null>(null);
  let copied = $state('');
  const origin = $derived(link === null ? null : viewerOrigin(link.url));

  async function open(): Promise<void> {
    error = null;
    try {
      link = await api.viewerLink(projectId, artifactId, revision, sha256);
    } catch (caught) {
      error = caught;
    }
  }
  void open();

  // Only the frame's own window, on the preview origin, and only about this PDF.
  $effect(() => {
    const shown = link;
    const from = origin;
    const element = frame;
    if (shown === null || from === null || element === null) return;
    const listen = (event: MessageEvent) => {
      const found = receivedAnchor(event, {
        origin: from,
        window: element.contentWindow,
        sha256,
        path: shown.path,
      });
      if (found !== null) {
        anchor = found;
        copied = '';
      }
    };
    window.addEventListener('message', listen);
    return () => window.removeEventListener('message', listen);
  });

  async function copy(): Promise<void> {
    if (anchor === null) return;
    try {
      await navigator.clipboard.writeText(anchorText(anchor));
      copied = 'Copied.';
    } catch {
      copied = 'The browser did not allow copying; select the anchor below and copy it.';
    }
  }
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
</nav>
<h1>{link?.path ?? 'PDF'}</h1>
<p class="muted">
  Revision {revision} · <code>{sha256}</code>{#if page !== null}
    · opened at page {page}{/if}
</p>

{#if error}
  <ErrorMessage {error} />
  <button type="button" onclick={open}>Try again</button>
{:else if link === null}
  <p class="muted">Opening…</p>
{:else if origin === null}
  <p class="problem">The server gave a viewer address that is not one.</p>
{:else}
  <p class="muted">
    The viewer's link opens this PDF until {formatTime(link.expires_at)}.
    <button type="button" onclick={open}>Open again</button>
  </p>
  <iframe
    bind:this={frame}
    class="pdf-frame"
    src={atPage(link.url, page)}
    sandbox="allow-scripts allow-same-origin"
    referrerpolicy="no-referrer"
    title={`PDF viewer: ${link.path}`}
  ></iframe>
{/if}

<h2>Anchor</h2>
{#if anchor === null}
  <p class="muted">Select text in the PDF and press Copy anchor: its anchor appears here.</p>
{:else}
  <p>
    Page {anchor.page.index}
    {#if anchor.page.label !== String(anchor.page.index)}(labelled {anchor.page.label}){/if}
    <button type="button" onclick={copy}>Copy</button>
    {#if copied}<span class="muted">{copied}</span>{/if}
  </p>
  {#if anchor.quote}<blockquote>{anchor.quote}</blockquote>{/if}
  <pre><code>{anchorText(anchor)}</code></pre>
{/if}
