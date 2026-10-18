<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { api, type DocumentRevision, type CommentThread, type Session } from '../lib/api';
  import { anchorOnShown, newComment, startComment } from '../lib/commentAnchors';
  import { commentDrafts } from '../lib/drafts';
  import { guardedWrite } from '../lib/mutation';
  import { macroSet } from '../lib/macros';
  import { emptyList, PagedList } from '../lib/pagedList';
  import { idle, submit } from '../lib/submit';
  import { visible } from '../lib/text';
  import type { ReaderAnchor } from '../lib/documentSelection';
  import Markdown from '../lib/Markdown.svelte';
  import AnnotationThread from './AnnotationThread.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  import HiddenBanner from './HiddenBanner.svelte';
  let {
    projectId,
    found,
    requestedAnchor = null,
    consumeAnchor,
  }: {
    projectId: string;
    found: DocumentRevision;
    requestedAnchor?: ReaderAnchor | null;
    consumeAnchor: () => void;
  } = $props();
  const initial = untrack(() => found);
  const id = $derived(found.document.document_id);
  const draftScope = untrack(() => `${projectId}:${initial.document.document_id}`);
  let draft = $state(startComment(initial, commentDrafts.load(draftScope).draft));
  let sessions = $state<Session[]>([]);
  let sessionsError = $state<unknown>(null);
  const threads = $state(emptyList<CommentThread, string>());
  const list = new PagedList(threads, (after) =>
    api
      .comments(projectId, id, found.revision.number, after)
      .then((page) => ({ items: page.threads, next: page.next_after })),
  );
  const posting = $state(idle());
  let persisted = $state(true);
  let commentField: HTMLTextAreaElement | undefined = $state();
  let anchorNote = $state('');
  const error = $derived(posting.error ?? threads.error);
  const macros = $derived(
    macroSet(found.macros, `${projectId}:${found.macro_context?.key ?? 'legacy-document'}`),
  );
  const anchorDocument = $derived(
    draft.base === found.revision.number
      ? Promise.resolve(found)
      : api.document(projectId, id, draft.base),
  );
  $effect(() => {
    if (draft.body) persisted = commentDrafts.save(draftScope, $state.snapshot(draft));
    else commentDrafts.clear(draftScope);
  });
  $effect(() => {
    const request = requestedAnchor;
    if (!request) return;
    consumeAnchor();
    if (request.documentId !== id || request.revision !== found.revision.number) return;
    draft.base = request.revision;
    draft.block = request.block;
    draft.kind = request.kind;
    draft.math = request.math;
    draft.quote = request.quote;
    anchorNote = request.note;
    void tick().then(() => commentField?.focus());
  });
  $effect(() => {
    void list.reload();
    api
      .project(projectId)
      .then((project) => {
        sessions = project.sessions;
        sessionsError = null;
      })
      .catch((caught) => (sessionsError = caught));
  });
  async function create(anchored: DocumentRevision) {
    const posted = await submit(posting, async () => {
      await guardedWrite('comment', `${projectId}:${id}`, newComment(anchored, draft), (sent) =>
        api.changeComment(projectId, id, sent),
      );
      discard();
    });
    if (posted) await list.reload();
  }
  function refresh() {
    posting.error = null;
    void list.reload();
  }
  function discard() {
    draft.body = '';
    anchorNote = '';
    commentDrafts.clear(draftScope);
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (draft.body && !persisted) event.preventDefault();
  }}
/>
<section class="comments" aria-label="Document comments">
  <p class="muted">
    Select text in the document, then comment here. Choose a session to ask its main agent to act.
  </p>
  <details class="delivery-help">
    <summary>How work requests are delivered</summary>
    <p>
      A running run takes it at its next safe point; an open session with no run starts one for it,
      as a chat message does. A closed session keeps it for its next run: a request does not reopen
      it.
    </p>
  </details>
  {#if error}<ErrorMessage {error} />{/if}
  <!-- The sessions a comment may be addressed to; failing to read them hides nothing else. -->
  {#if sessionsError}<ErrorMessage error={sessionsError} />{/if}
  {#if found.matched && !found.revision.tombstone}
    <h3>New comment</h3>
    {#if anchorNote}<p role="status">{anchorNote}</p>{/if}
    {#await anchorDocument}
      <p>Loading anchored revision…</p>
    {:then anchored}
      {@const selected = anchored.blocks.find((item) => item.id === draft.block)}
      <fieldset disabled={posting.busy}>
        <p class="anchor-summary">
          Revision {draft.base} · {draft.kind === 'math'
            ? `formula ${draft.math}`
            : draft.kind === 'range'
              ? 'selected text'
              : `block ${draft.block}`}
        </p>
        <label
          >Comment Markdown <textarea
            aria-label="Comment Markdown"
            bind:this={commentField}
            bind:value={draft.body}
            rows="5"
            maxlength="8192"></textarea></label
        >
        <HiddenBanner text={draft.body} />
        <label
          >Send work request to <select bind:value={draft.session}
            ><option value="">No work request</option
            >{#each sessions as item (item.session_id)}<option value={item.session_id}
                >{item.session_id}{item.closed_at
                  ? ' (closed: waits for its next run)'
                  : ''}</option
              >{/each}</select
          ></label
        >
        <p class="muted">
          {draft.session
            ? 'The main agent receives this request during a run, or starts a run if the session is open and idle.'
            : 'This comment stays in the document without sending a work request.'}
        </p>
        <button disabled={!draft.body.trim()} onclick={() => create(anchored)}
          >{draft.session ? 'Post and send request' : 'Post comment'}</button
        >
        <button onclick={discard}>Discard draft</button>
        <details class="anchor-options">
          <summary>Change anchor</summary>
          {#if draft.base !== found.revision.number}<button
              disabled={posting.busy}
              onclick={() => {
                draft = anchorOnShown(draft, found);
                anchorNote = '';
              }}>Use displayed revision for the anchor</button
            >{/if}
          <label
            >Anchor block <select
              bind:value={draft.block}
              onchange={() => {
                draft.math = '';
                draft.quote = '';
                anchorNote = '';
              }}
              >{#each anchored.blocks as item (item.id)}<option value={item.id}
                  >{item.id} · {item.kind}</option
                >{/each}</select
            ></label
          >
          <label
            >Anchor kind <select bind:value={draft.kind} onchange={() => (anchorNote = '')}
              ><option value="block">Whole block</option><option value="math">Formula</option
              ><option value="range">Quoted text range</option></select
            ></label
          >
          {#if draft.kind === 'math'}<label
              >Formula <select bind:value={draft.math}
                ><option value="">Choose formula</option
                >{#each selected?.math ?? [] as span (span.id)}<option value={span.id}
                    >{span.id} · {(span.tex ?? '').slice(0, 100)}</option
                  >{/each}</select
              ></label
            >{/if}
          {#if draft.kind === 'range'}<label
              >Exact quoted text <input bind:value={draft.quote} maxlength="4096" /></label
            >{/if}
          <details>
            <summary>Selected block source</summary>
            <pre>{visible(selected?.source ?? '')}</pre>
          </details>
        </details>
      </fieldset>
    {:catch caught}<ErrorMessage error={caught} />{/await}
    {#if !persisted}<p role="alert">
        Browser storage is unavailable. Keep this tab open or copy your comment draft.
      </p>{/if}
    {#if draft.body}<details>
        <summary>Comment preview</summary><Markdown source={draft.body} {macros} />
      </details>{/if}
  {/if}
  <div class="thread-heading">
    <h3>Threads</h3>
    <button disabled={threads.loading || posting.busy} onclick={refresh}>Refresh</button>
  </div>
  {#if threads.items.length === 0 && !threads.loading}<p class="muted">
      No comments on this revision.
    </p>{/if}
  {#each threads.items as thread (thread.thread_id)}
    <AnnotationThread
      {projectId}
      documentId={id}
      {thread}
      {sessions}
      {macros}
      changed={() => list.reload()}
    />
  {/each}
  {#if threads.next}<button disabled={threads.loading} onclick={() => list.more()}
      >Load more threads</button
    >{/if}
</section>

<style>
  .comments {
    min-width: 0;
  }
  .delivery-help {
    color: var(--muted);
    font-size: 0.85rem;
  }
  .thread-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    margin-top: 1.5rem;
  }
  .thread-heading h3 {
    margin: 0;
  }
  .anchor-summary {
    color: var(--muted);
    font-size: 0.85rem;
    margin-bottom: 0;
  }
  .anchor-options {
    border-top: 1px solid var(--border);
    margin-top: 1rem;
    padding-top: 0.75rem;
  }
  label {
    display: block;
    margin: 0.6rem 0;
  }
  textarea {
    width: 100%;
    box-sizing: border-box;
  }
  select,
  input {
    max-width: 100%;
    box-sizing: border-box;
  }
  select {
    width: 100%;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 16rem;
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
