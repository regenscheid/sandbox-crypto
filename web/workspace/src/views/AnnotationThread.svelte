<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, type CommentEntry, type CommentThread, type Session } from '../lib/api';
  import { describeAuthor } from '../lib/authors';
  import { afterPost, earlierEntries, threadChange, threadEntries } from '../lib/commentThreads';
  import { Earlier } from '../lib/earlier';
  import { replyDrafts } from '../lib/drafts';
  import { guardedWrite } from '../lib/mutation';
  import type { MacroSet } from '../lib/macros';
  import { emptyList } from '../lib/pagedList';
  import { idle, submit } from '../lib/submit';
  import Markdown from '../lib/Markdown.svelte';
  import { documentHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import CommentBodyEditor from './CommentBodyEditor.svelte';
  let {
    projectId,
    documentId,
    thread,
    sessions,
    macros,
    changed,
  }: {
    projectId: string;
    documentId: string;
    thread: CommentThread;
    sessions: Session[];
    macros: MacroSet;
    changed: () => Promise<void>;
  } = $props();
  const initial = untrack(() => thread);
  const draftScope = untrack(() => `${projectId}:${documentId}:${initial.thread_id}`);
  let draft = $state(
    replyDrafts.load(draftScope).draft ?? {
      expected: initial.revision,
      body: '',
      mode: 'reply' as const,
      session: '',
    },
  );
  const posting = $state(idle());
  const read = $state(emptyList<CommentEntry[], number>());
  const earlier = new Earlier(
    read,
    earlierEntries(
      () => thread,
      (before) => api.commentThread(projectId, documentId, thread.thread_id, before),
    ),
  );
  let persisted = $state(true);
  let historyRevision = initial.revision;
  const busy = $derived(posting.busy || read.loading);
  const error = $derived(posting.error ?? read.error);
  $effect(() => {
    // A new thread revision starts the earlier entries again.
    if (thread.revision !== historyRevision) {
      earlier.reset();
      historyRevision = thread.revision;
    }
  });
  $effect(() => {
    if (draft.body) persisted = replyDrafts.save(draftScope, $state.snapshot(draft));
    else {
      replyDrafts.clear(draftScope);
      draft.expected = thread.revision;
    }
  });
  async function mutate(action: string) {
    await submit(posting, async () => {
      const result = await guardedWrite(
        'comment',
        `${projectId}:${documentId}`,
        threadChange(action, thread, draft),
        (sent) => api.changeComment(projectId, documentId, sent),
      );
      if (action === 'reply' || action === 'edit') {
        draft = afterPost(draft, result);
        replyDrafts.clear(draftScope);
      }
      await changed();
    });
  }
  function discard() {
    draft.body = '';
    draft.expected = thread.revision;
    replyDrafts.clear(draftScope);
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (draft.body && !persisted) event.preventDefault();
  }}
/>
<article class="thread">
  <h3>
    {thread.anchor.block}{thread.anchor.math ? ` / ${thread.anchor.math}` : ''} · {thread.attachment
      .status} · {thread.status}
  </h3>
  <p class="muted">
    Anchored to <a href={documentHref(projectId, documentId, thread.anchor.revision)}
      >revision {thread.anchor.revision}</a
    >; thread revision {thread.revision}. {describeAuthor(thread.opener)}
  </p>
  <p class="muted">
    Thread history uses the displayed document's macro context for diagnostic rendering. Immutable
    math-check evidence retains the exact context checked.
  </p>
  <details open={thread.attachment.status === 'stale' || thread.attachment.status === 'orphaned'}>
    <summary
      >Original quoted context{thread.anchor.quote_truncated ? ' (bounded excerpt)' : ''}</summary
    >
    {#if thread.anchor.before}<pre>{thread.anchor.before}</pre>{/if}
    <blockquote><pre>{thread.anchor.quote}</pre></blockquote>
    {#if thread.anchor.after}<pre>{thread.anchor.after}</pre>{/if}
    {#if thread.attachment.status === 'stale'}<h4>Current text (bounded excerpt)</h4>
      <pre>{thread.attachment.current}</pre>{/if}
  </details>
  {#if earlier.available(thread.entries_before !== null)}<button
      disabled={busy}
      onclick={() => earlier.read()}>Load earlier thread entries</button
    >{/if}
  {#each threadEntries(earlier.entries(), thread.entries) as entry (entry.entry_id)}
    <div class="entry">
      <p class="muted">
        {entry.number} · {entry.kind} · {describeAuthor(entry.author)} · {new Date(
          entry.created_at * 1000,
        ).toLocaleString()}
      </p>
      {#if entry.body}<Markdown source={entry.body} {macros} />{/if}
      {#if entry.body_source}
        {#if entry.body_source.held}<p role="status">
            Body math checks held: {entry.body_source.held}
          </p>
        {/if}
        {#if entry.body_source.revision !== undefined}
          <details>
            <summary
              >Current body revision {entry.body_source.revision} · {entry.body_source.author
                ? describeAuthor(entry.body_source.author)
                : 'legacy ownership unknown'}</summary
            ><Markdown source={entry.body_source.body ?? ''} {macros} />
          </details>
          {#if entry.author.kind === 'user'}<CommentBodyEditor
              {projectId}
              {documentId}
              threadId={thread.thread_id}
              expected={thread.revision}
              source={entry.body_source}
              {changed}
            />{/if}
        {/if}
      {/if}
      {#if entry.payload.entry}<p class="muted">
          Correction of original entry <code>{entry.payload.entry}</code>, body revision {entry
            .payload.body_revision}.
        </p>{/if}
      {#if entry.payload.revision}<p>
          Made <a href={documentHref(projectId, documentId, entry.payload.revision)}
            >revision {entry.payload.revision}</a
          >; changed blocks: {(entry.payload.changed_blocks ?? []).join(', ') || 'none'}. Outside
          the anchor (including changes whose scope cannot be proved): {(
            entry.payload.outside_anchor ?? []
          ).join(', ') || 'none'}.
        </p>{/if}
      {#if entry.payload.changed_blocks_truncated || entry.payload.outside_anchor_truncated}<p
          class="muted"
        >
          Block lists show the first 100 IDs: {entry.payload.changed_blocks_total} changed, {entry
            .payload.outside_anchor_total} outside or uncertain. The revision and its history hold the
          full changes.
        </p>{/if}
      {#if entry.payload.addressed_to}<p>Addressed to session {entry.payload.addressed_to}.</p>{/if}
    </div>
  {/each}
  {#each thread.requests as request (request.request_id)}<p class="muted">
      Request to {request.session_id}: {request.state}{request.delivered_run
        ? ` in ${request.delivered_run}`
        : ''}.
    </p>{/each}
  <button disabled={busy} onclick={() => mutate(thread.status === 'open' ? 'resolve' : 'reopen')}
    >{thread.status === 'open' ? 'Resolve thread' : 'Reopen thread'}</button
  >
  <details>
    <summary>Reply{thread.opener.kind === 'user' ? ' or edit your opening comment' : ''}</summary>
    <fieldset disabled={busy}>
      <label
        >Action <select bind:value={draft.mode}
          ><option value="reply">Reply</option>{#if thread.opener.kind === 'user'}<option
              value="edit">Append an edit to your opening comment</option
            >{/if}</select
        ></label
      >
      <p>
        Based on thread revision {draft.expected}. {#if draft.expected !== thread.revision}<button
            onclick={() => (draft.expected = thread.revision)}>Use current thread revision</button
          > Your draft is retained.{/if}
      </p>
      <label
        >Reply Markdown<textarea
          aria-label={`Reply ${thread.thread_id}`}
          bind:value={draft.body}
          maxlength="8192"
          rows="4"></textarea></label
      >
      <label
        >Address to main agent <select bind:value={draft.session}
          ><option value="">No work request</option
          >{#each sessions as item (item.session_id)}<option value={item.session_id}
              >{item.session_id}</option
            >{/each}</select
        ></label
      >
      <button disabled={!draft.body.trim()} onclick={() => mutate(draft.mode)}
        >Post {draft.mode}</button
      ><button onclick={discard}>Discard reply draft</button>
    </fieldset>
  </details>
  {#if !persisted}<p role="alert">
      Browser storage is unavailable. Keep this tab open or copy your reply draft.
    </p>{/if}
  {#if error}<ErrorMessage {error} />{/if}
</article>

<style>
  .thread {
    border: 1px solid var(--border, #9996);
    border-radius: 0.4rem;
    margin: 1rem 0;
    padding: 1rem;
  }
  .entry {
    border-top: 1px solid var(--border, #9996);
    padding-top: 0.3rem;
  }
  label {
    display: block;
    margin: 0.6rem 0;
  }
  textarea {
    width: 100%;
    box-sizing: border-box;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 12rem;
    overflow: auto;
  }
  fieldset {
    border: 0;
    padding: 0;
  }
  button {
    margin: 0.25rem 0.5rem 0.25rem 0;
  }
</style>
