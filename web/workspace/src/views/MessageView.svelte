<!-- SPDX-License-Identifier: MIT -->
<!-- One archived message, read whole and read-only (docs/specs/context-management.md,
     CM-3, CM-34): its place, its run and its text as `history_read` gives it, shown as
     text, each character a person may not see as its code point. The review views link
     here from the items that name an archive place (CR-14 as amended). -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, type ArchivedMessage } from '../lib/api';
  import { formatTime } from '../lib/format';
  import { projectHref, runHref, sessionHref } from '../lib/router';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId, messageId }: { projectId: string; messageId: string } = $props();
  // The route keys this view by the project and message: they never change here.
  const [project, id] = untrack(() => [projectId, messageId]);
  let found = $state<ArchivedMessage | null>(null);
  let error = $state<unknown>(null);
  api.message(project, id).then(
    (message) => (found = message),
    (failed: unknown) => (error = failed),
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
  <p class="muted">Reading the message…</p>
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
