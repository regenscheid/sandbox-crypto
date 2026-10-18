<!-- SPDX-License-Identifier: MIT -->
<!-- One archived message, read whole and read-only (docs/specs/context-management.md,
     CM-3, CM-34): its place, its run and its text as `history_read` gives it, shown as
     text, each character a person may not see as its code point. The review views link
     here from the items that name an archive place (CR-14 as amended). -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, ApiError, type ArchivedMessage } from '../lib/api';
  import { formatTime } from '../lib/format';
  import { projectHref, runHref, sessionHref } from '../lib/router';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId, messageId }: { projectId: string; messageId: string } = $props();
  // The route keys this view by the project and message: they never change here.
  const [project, id] = untrack(() => [projectId, messageId]);
  let found = $state<ArchivedMessage | null>(null);
  let error = $state<unknown>(null);
  let large = $state(false);
  let field = $state<'content' | 'tool_calls'>('content');
  let rawText = $state('');
  let rawTotal = $state(0);
  let rawNext = $state<number | null>(0);
  let rawLoading = $state(false);
  let rawError = $state<unknown>(null);
  let decoder = new TextDecoder('utf-8', { fatal: true });

  async function loadChunks() {
    if (rawLoading || rawNext === null) return;
    rawLoading = true;
    rawError = null;
    rawText = '';
    try {
      // One click reads at most 1 MiB. Keep decoding state across UTF-8 byte boundaries.
      for (let count = 0; count < 16 && rawNext !== null; count++) {
        const page = await api.messageChunk(project, id, field, rawNext);
        const binary = atob(page.data_base64);
        const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
        rawText += decoder.decode(bytes, { stream: page.next_offset !== null });
        rawTotal = page.total;
        rawNext = page.next_offset;
      }
    } catch (failed) {
      rawError = failed;
    } finally {
      rawLoading = false;
    }
  }

  function showField(next: typeof field) {
    if (rawLoading) return;
    field = next;
    rawText = '';
    rawTotal = 0;
    rawNext = 0;
    decoder = new TextDecoder('utf-8', { fatal: true });
    void loadChunks();
  }
  api.message(project, id).then(
    (message) => (found = message),
    (failed: unknown) => {
      if (failed instanceof ApiError && failed.status === 422 && failed.kind === 'unsupported') {
        large = true;
        void loadChunks();
      } else error = failed;
    },
  );
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(project)}>{project}</a>
  {#if found}/ <a href={sessionHref(project, found.session)}>{visible(found.session)}</a>{/if} /
</nav>
<h1>Archived message <code>{id}</code></h1>
{#if error}
  <ErrorMessage {error} />
{:else if found === null}
  {#if large}
    <p class="muted">
      This message exceeds the whole-message read limit. The exact archived {field === 'content'
        ? 'content'
        : 'tool-call JSON'} is available in byte pages, shown as plain text.
    </p>
    <p>
      {field}: {rawNext === null ? rawTotal : rawNext} of {rawTotal} bytes read. Showing only the latest
      page (at most 1 MiB).
    </p>
    {#if rawError}<ErrorMessage error={rawError} />{/if}
    <pre class="message">{visible(rawText)}</pre>
    {#if rawNext !== null}<button
        type="button"
        disabled={rawLoading}
        onclick={() => void loadChunks()}>{rawLoading ? 'Reading…' : 'Read next 1 MiB'}</button
      >{/if}
    {#if field === 'content'}<button
        type="button"
        disabled={rawLoading}
        onclick={() => showField('tool_calls')}>Read stored tool calls</button
      >{/if}
    {#if field === 'tool_calls'}<button
        type="button"
        disabled={rawLoading}
        onclick={() => showField('content')}>Read content</button
      >{/if}
    {#if rawNext !== 0}<button type="button" disabled={rawLoading} onclick={() => showField(field)}
        >Restart this field</button
      >{/if}
  {:else}<p class="muted">Reading the message…</p>{/if}
{:else}
  <dl class="facts">
    <dt>Kind</dt>
    <dd>
      {visible(found.kind)}{found.source ? `, ${visible(found.source)}` : ''}{found.call_id
        ? ` (call ${visible(found.call_id)})`
        : ''}
    </dd>
    <dt>Run</dt>
    <dd>
      <a href={runHref(project, found.session, found.run)}>{visible(found.run)}</a
      >{#if found.parent}, a delegated run of <a
          href={runHref(project, found.session, found.parent)}>{visible(found.parent)}</a
        >{/if}
    </dd>
    <dt>Position</dt>
    <dd>{found.position} in its session</dd>
    <dt>Kept</dt>
    <dd>{formatTime(found.created)}</dd>
  </dl>
  <p class="muted">As the run added it, read-only; an answer's tool calls follow its text.</p>
  <pre class="message">{visible(found.text)}</pre>
{/if}

<style>
  pre.message {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
</style>
