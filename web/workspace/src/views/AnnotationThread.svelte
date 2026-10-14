<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, type CommentThread, type CommentEntry, type Session } from '../lib/api';
  import { describeAuthor } from '../lib/authors';
  import { documentRequest, documentAnswered } from '../lib/documentRequests';
  import { loadDraft, saveDraft, clearDraft } from '../lib/drafts';
  import type { MacroSet } from '../lib/macros';
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
  const draftKey = untrack(() => `${documentId}:reply:${initial.thread_id}`);
  const draft = untrack(() => loadDraft(projectId, draftKey));
  let body = $state(draft?.text ?? '');
  let expected = $state(draft?.base ?? initial.revision);
  let session = $state(draft?.retained ?? '');
  let mode = $state(draft?.reason === 'edit' ? 'edit' : 'reply');
  let busy = $state(false);
  let error = $state<unknown>(null);
  let persisted = $state(true);
  let older = $state<CommentEntry[]>([]);
  let before = $state(initial.entries_before);
  let historyRevision = $state(initial.revision);
  $effect(() => {
    if (thread.revision !== historyRevision) {
      older = [];
      before = thread.entries_before;
      historyRevision = thread.revision;
    }
  });
  $effect(() => {
    if (body)
      persisted = saveDraft(projectId, draftKey, {
        base: expected,
        text: body,
        reason: mode,
        block: null,
        retained: session,
      });
    else {
      clearDraft(projectId, draftKey);
      expected = thread.revision;
    }
  });
  async function mutate(action: string) {
    busy = true;
    error = null;
    try {
      const writing = action === 'reply' || action === 'edit';
      const change = {
        action,
        thread: thread.thread_id,
        expected: writing ? expected : thread.revision,
        ...(writing ? { body, ...(session ? { addressed_to: session } : {}) } : {}),
      };
      const request = await documentRequest(projectId, documentId, change);
      const result = await api.changeComment(projectId, documentId, {
        ...change,
        operation_id: request.id,
      });
      documentAnswered(request);
      if (writing) {
        body = '';
        expected = result.revision;
        clearDraft(projectId, draftKey);
      }
      await changed();
    } catch (caught) {
      error = caught;
    } finally {
      busy = false;
    }
  }
  async function history() {
    busy = true;
    try {
      const head = thread.revision;
      const result = await api.commentThread(projectId, documentId, thread.thread_id, before);
      if (head !== thread.revision) return;
      older = [...result.entries, ...older];
      before = result.entries_before;
    } catch (caught) {
      error = caught;
    } finally {
      busy = false;
    }
  }
  function discard() {
    body = '';
    expected = thread.revision;
    clearDraft(projectId, draftKey);
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (body && !persisted) event.preventDefault();
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
  {#if before}<button disabled={busy} onclick={history}>Load earlier thread entries</button>{/if}
  {#each [...older, ...thread.entries].filter((entry, index, all) => all.findIndex((item) => item.entry_id === entry.entry_id) === index) as entry (entry.entry_id)}
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
        >Action <select bind:value={mode}
          ><option value="reply">Reply</option>{#if thread.opener.kind === 'user'}<option
              value="edit">Append an edit to your opening comment</option
            >{/if}</select
        ></label
      >
      <p>
        Based on thread revision {expected}. {#if expected !== thread.revision}<button
            onclick={() => (expected = thread.revision)}>Use current thread revision</button
          > Your draft is retained.{/if}
      </p>
      <label
        >Reply Markdown<textarea
          aria-label={`Reply ${thread.thread_id}`}
          bind:value={body}
          maxlength="8192"
          rows="4"></textarea></label
      >
      <label
        >Address to main agent <select bind:value={session}
          ><option value="">No work request</option
          >{#each sessions as item (item.session_id)}<option value={item.session_id}
              >{item.session_id}</option
            >{/each}</select
        ></label
      >
      <button disabled={!body.trim()} onclick={() => mutate(mode)}>Post {mode}</button><button
        onclick={discard}>Discard reply draft</button
      >
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
