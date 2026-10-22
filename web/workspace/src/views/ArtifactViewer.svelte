<!-- SPDX-License-Identifier: MIT -->
<!-- One artifact revision, opened in the Artifacts page: an image or a figure from the
     preview origin (figures, FG-12, FG-14), a PDF through the page viewer, Markdown
     rendered, and other text as text with line numbers, its code highlighted and its JSON
     laid out, read from its kept bytes up to a limit. The file's text is the agents' and is
     never rendered as HTML outside the Markdown component. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { ArtifactEntry } from '../lib/api-types';
  import { emptyList, PagedList } from '../lib/pagedList';
  import { kindText, splitPath, textual } from '../lib/artifactGrid';
  import { keptText, whoText } from '../lib/artifactFiles';
  import { shownAsImage, shownInFrame } from '../lib/figures';
  import { formatTime } from '../lib/format';
  import { formatJson, highlight, languageOf } from '../lib/highlight';
  import Markdown from '../lib/Markdown.svelte';
  import { sizeText, viewable } from '../lib/pdfAnchors';
  import { pdfHref, sessionHref } from '../lib/router';
  import { visibleOutput } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';
  import FigureFrame from './FigureFrame.svelte';
  import FigureImage from './FigureImage.svelte';

  /** The most bytes of a text file the viewer reads. */
  const TEXT_LIMIT = 512 * 1024;

  let {
    projectId,
    entry,
    viewer,
    close,
    choose,
  }: {
    projectId: string;
    entry: ArtifactEntry;
    viewer: boolean;
    close: () => void;
    /** Show another revision of the same artifact. */
    choose: (revision: ArtifactEntry) => void;
  } = $props();
  // The artifact's revisions, the newest first, a page at a time (RA-37).
  const revisions = $state(emptyList<ArtifactEntry, number>());
  const history = new PagedList(revisions, async (before) => {
    const page = await api.artifactRevisions(projectId, entry.artifact_id, before);
    return { items: page.revisions, next: page.next_before };
  });
  // Through `$derived`, so only another artifact restarts its history, not an equal entry.
  const artifactId = $derived(entry.artifact_id);
  $effect(() => {
    void artifactId;
    // Its first read reads `entry` itself: only `artifactId` says when to read again.
    void untrack(() => history.restart());
  });
  const download = $derived(
    `/api/v0/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(entry.artifact_id)}/revisions/${entry.revision}/content?sha256=${encodeURIComponent(entry.sha256)}`,
  );
  const parts = $derived(splitPath(entry.path));
  const image = $derived(shownAsImage(entry, viewer));
  const framed = $derived(shownInFrame(entry, viewer));
  const pdf = $derived(!entry.deleted && entry.content_retained && viewable(entry, viewer));
  const readable = $derived(
    !entry.deleted && entry.content_retained && textual(entry) && entry.size <= TEXT_LIMIT,
  );
  const markdown = $derived(/\.(md|markdown)$/i.test(entry.path));
  let source = $state(false);
  let text = $state<string | null>(null);
  const language = $derived(languageOf(entry.path));
  /** JSON laid out for reading, when it is one value not already so; null otherwise. */
  const formatted = $derived.by(() => {
    if (language !== 'json' || text === null) return null;
    const laid = formatJson(text);
    return laid === null || laid.trimEnd() === text.trimEnd() ? null : laid;
  });
  let asSaved = $state(false);
  const shown = $derived(formatted !== null && !asSaved ? formatted : (text ?? ''));
  const tokens = $derived(highlight(shown, language));
  const lines = $derived.by(() => {
    const count = shown.split('\n').length - (shown.endsWith('\n') ? 1 : 0);
    return Array.from({ length: Math.max(count, 1) }, (_, index) => index + 1).join('\n');
  });
  let failure = $state<unknown>(null);

  $effect(() => {
    const url = download,
      wanted = readable;
    text = null;
    failure = null;
    if (!wanted) return;
    const controller = new AbortController();
    fetch(url, { signal: controller.signal, credentials: 'same-origin' })
      .then(async (response) => {
        if (!response.ok) throw new Error(`The file could not be read (HTTP ${response.status}).`);
        text = await response.text();
      })
      .catch((error) => {
        if (!controller.signal.aborted) failure = error;
      });
    return () => controller.abort();
  });
</script>

<section class="artifact-viewer" aria-label="File viewer">
  <div class="viewer-bar">
    <button type="button" class="link-button" onclick={close}>← Artifacts</button>
    <div class="viewer-title">
      <strong><bdi>{parts.name}</bdi></strong>
      <small class="muted"
        ><bdi>{parts.folder || '/'}</bdi> · {kindText(entry)} · revision {entry.revision}</small
      >
    </div>
    {#if markdown && text !== null}<button type="button" onclick={() => (source = !source)}
        >{source ? 'Rendered' : 'Source'}</button
      >{/if}
    {#if formatted !== null}<button
        type="button"
        aria-pressed={!asSaved}
        title="Laid out for reading; every value as saved"
        onclick={() => (asSaved = !asSaved)}>{asSaved ? 'Format' : 'As saved'}</button
      >{/if}
    {#if pdf}<a
        class="button-link"
        href={pdfHref(projectId, entry.artifact_id, entry.revision, entry.sha256)}
        >Open in the PDF viewer</a
      >{/if}
    {#if entry.content_retained && !entry.deleted}<a class="button-link" href={download} download
        >Download</a
      >{/if}
  </div>
  <div class="viewer-body">
    <div class="viewer-content">
      {#if keptText(entry)}
        <p class="empty-note">This revision's {keptText(entry)}.</p>
      {:else if image}
        <FigureImage
          {projectId}
          artifact={entry.artifact_id}
          revision={entry.revision}
          sha256={entry.sha256}
          alt={entry.path}
        />
      {:else if framed}
        <FigureFrame
          {projectId}
          artifact={entry.artifact_id}
          revision={entry.revision}
          sha256={entry.sha256}
          alt={entry.path}
        />
      {:else if readable}
        {#if failure}<ErrorMessage error={failure} />{:else if text === null}<p class="muted">
            Reading the file…
          </p>{:else if markdown && !source}<div class="markdown-file">
            <Markdown source={text} {projectId} />
          </div>{:else if text === ''}<p class="empty-note">The file is empty.</p>{:else}<div
            class="code-view"
          >
            <pre class="gutter" aria-hidden="true">{lines}</pre>
            <pre
              class="file-text">{#each tokens as token, index (index)}{#if token.kind === 'plain'}{visibleOutput(
                    token.text,
                  )}{:else}<span class="tok-{token.kind}">{visibleOutput(token.text)}</span
                  >{/if}{/each}</pre>
          </div>{/if}
      {:else if pdf}
        <p class="empty-note">Open the PDF in the page viewer to read it.</p>
      {:else}
        <p class="empty-note">
          {entry.size > TEXT_LIMIT && textual(entry)
            ? `The file is ${sizeText(entry.size)}, more than the viewer reads; download it to read it.`
            : 'This kind of file has no preview here; download it to open it.'}
        </p>
      {/if}
    </div>
    <aside class="viewer-facts">
      <span class="eyebrow">About this file</span>
      <dl class="facts">
        <dt>Size</dt>
        <dd>{entry.deleted ? '—' : sizeText(entry.size)}</dd>
        <dt>Written</dt>
        <dd>{formatTime(entry.created_at)}</dd>
        <dt>By</dt>
        <dd><bdi>{whoText(entry)}</bdi></dd>
        <dt>Session</dt>
        <dd><a href={sessionHref(projectId, entry.session_id)}>{entry.session_id}</a></dd>
        <dt>Result</dt>
        <dd>{entry.result ? 'Yes' : 'No'}</dd>
        <dt>SHA-256</dt>
        <dd><code>{entry.sha256.slice(0, 16)}…</code></dd>
      </dl>
      <span class="eyebrow">Revisions</span>
      <ol class="revision-list">
        {#each revisions.items as revision (revision.revision)}
          <li>
            <button
              type="button"
              class="revision"
              aria-current={revision.revision === entry.revision ? 'true' : undefined}
              onclick={() => choose(revision)}
              ><strong>{revision.revision}</strong><span
                >{revision.deleted
                  ? 'deleted'
                  : `${sizeText(revision.size)} · ${formatTime(revision.created_at)}`}</span
              ></button
            >
          </li>
        {/each}
      </ol>
      {#if revisions.loading}<p class="muted">Loading…</p>{/if}
      {#if revisions.error}<ErrorMessage error={revisions.error} />{/if}
      {#if revisions.next !== null}<button
          type="button"
          disabled={revisions.loading}
          onclick={() => history.more()}>Older revisions</button
        >{/if}
    </aside>
  </div>
</section>

<style>
  .artifact-viewer {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .viewer-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    padding: 10px 0;
    border-bottom: 1px solid var(--subtle);
  }
  .viewer-title {
    flex: 1 1 260px;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .viewer-title strong {
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .viewer-body {
    display: flex;
    flex-wrap: wrap;
    gap: 24px;
    padding: 20px 0;
  }
  .viewer-content {
    flex: 999 1 520px;
    min-width: 0;
  }
  .viewer-facts {
    flex: 1 1 220px;
    max-width: 300px;
    min-width: 0;
    font-size: 13px;
  }
  .code-view {
    display: flex;
    align-items: flex-start;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: var(--panel);
    overflow: hidden;
  }
  .code-view pre {
    margin: 0;
    padding: 12px 14px;
    font-size: 12.5px;
    line-height: 1.55;
    border: 0;
    border-radius: 0;
    background: transparent;
  }
  .gutter {
    flex: none;
    text-align: right;
    color: var(--muted);
    user-select: none;
    border-right: 1px solid var(--border) !important;
    padding-right: 10px !important;
  }
  .file-text {
    flex: 1 1 auto;
    min-width: 0;
    max-height: none;
    overflow-x: auto;
    white-space: pre;
  }
  .tok-keyword {
    color: var(--code-keyword);
  }
  .tok-string,
  .tok-key {
    color: var(--code-string);
  }
  .tok-key {
    font-weight: 600;
  }
  .tok-number,
  .tok-literal,
  .tok-variable {
    color: var(--code-number);
  }
  .tok-function,
  .tok-builtin {
    color: var(--code-function);
  }
  .tok-comment {
    color: var(--code-comment);
    font-style: italic;
  }
  .tok-punctuation {
    color: var(--muted);
  }
  .markdown-file {
    max-width: 760px;
  }
  .revision-list {
    list-style: none;
    padding: 0;
    margin: 6px 0 10px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .revision {
    width: 100%;
    display: flex;
    gap: 10px;
    align-items: baseline;
    min-height: 0;
    padding: 6px 8px;
    border: 0;
    background: transparent;
    text-align: left;
    font-size: 12px;
    color: var(--body-text);
  }
  .revision strong {
    width: 28px;
    flex: none;
    font-weight: 600;
  }
  .revision[aria-current='true'] {
    background: var(--subtle);
  }
</style>
