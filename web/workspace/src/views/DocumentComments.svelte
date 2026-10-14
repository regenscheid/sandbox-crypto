<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, type DocumentRevision, type CommentThread, type Session } from '../lib/api';
  import { anchorOnShown, newComment, startComment } from '../lib/commentAnchors';
  import { commentDrafts } from '../lib/drafts';
  import { guardedWrite } from '../lib/mutation';
  import { macroSet } from '../lib/macros';
  import { emptyList, PagedList } from '../lib/pagedList';
  import { idle, submit } from '../lib/submit';
  import Markdown from '../lib/Markdown.svelte';
  import AnnotationThread from './AnnotationThread.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  let { projectId, found }: { projectId: string; found: DocumentRevision } = $props();
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
  const error = $derived(posting.error ?? threads.error ?? sessionsError);
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
    void list.reload();
    api
      .project(projectId)
      .then((project) => (sessions = project.sessions))
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
  function discard() {
    draft.body = '';
    commentDrafts.clear(draftScope);
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (draft.body && !persisted) event.preventDefault();
  }}
/>
<section class="comments" aria-label="Document comments">
  <h2>Comments</h2>
  <p class="muted">
    Comments keep their original revision and quote. Address a comment to a session to queue a
    request for its main agent. An active run receives it at its next safe point; an idle session
    keeps it for its next manually started run.
  </p>
  <button disabled={threads.loading || posting.busy} onclick={() => list.reload()}
    >Refresh comments</button
  >
  {#if error}<ErrorMessage {error} />{/if}
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
  {#if found.matched && !found.revision.tombstone}
    <h3>Add a comment</h3>
    <p>
      Anchor revision {draft.base}. {#if draft.base !== found.revision.number}<button
          disabled={posting.busy}
          onclick={() => (draft = anchorOnShown(draft, found))}
          >Use displayed revision for the anchor</button
        >{/if}
    </p>
    {#await anchorDocument}
      <p>Loading anchored revision…</p>
    {:then anchored}
      {@const selected = anchored.blocks.find((item) => item.id === draft.block)}
      <fieldset disabled={posting.busy}>
        <label
          >Anchor block <select
            bind:value={draft.block}
            onchange={() => {
              draft.math = '';
              draft.quote = '';
            }}
            >{#each anchored.blocks as item (item.id)}<option value={item.id}
                >{item.id} · {item.kind}</option
              >{/each}</select
          ></label
        >
        <label
          >Anchor kind <select bind:value={draft.kind}
            ><option value="block">Whole block</option><option value="math">Formula</option><option
              value="range">Quoted text range</option
            ></select
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
          <pre>{selected?.source ?? ''}</pre>
        </details>
        <label
          >Comment Markdown <textarea
            aria-label="Comment Markdown"
            bind:value={draft.body}
            rows="5"
            maxlength="8192"></textarea></label
        >
        <label
          >Address to main agent <select bind:value={draft.session}
            ><option value="">No work request</option
            >{#each sessions as item (item.session_id)}<option value={item.session_id}
                >{item.session_id}{item.closed_at ? ' (closed; next run)' : ''}</option
              >{/each}</select
          ></label
        >
        <button disabled={!draft.body.trim()} onclick={() => create(anchored)}>Post comment</button>
        <button onclick={discard}>Discard comment draft</button>
      </fieldset>
    {:catch caught}<ErrorMessage error={caught} />{/await}
    {#if !persisted}<p role="alert">
        Browser storage is unavailable. Keep this tab open or copy your comment draft.
      </p>{/if}
    {#if draft.body}<details>
        <summary>Comment preview</summary><Markdown source={draft.body} {macros} />
      </details>{/if}
  {/if}
</section>

<style>
  .comments {
    margin-top: 2rem;
    border-top: 1px solid var(--border, #9996);
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
