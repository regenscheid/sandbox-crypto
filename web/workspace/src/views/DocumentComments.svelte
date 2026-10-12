<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, type DocumentRevision, type CommentThread, type Session } from '../lib/api';
  import { commentAnchor } from '../lib/commentAnchors';
  import { loadDraft, saveDraft, clearDraft } from '../lib/drafts';
  import { documentRequest, documentAnswered } from '../lib/documentRequests';
  import { macroSet } from '../lib/macros';
  import Markdown from '../lib/Markdown.svelte';
  import AnnotationThread from './AnnotationThread.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  let { projectId, found }: { projectId: string; found: DocumentRevision } = $props();
  const initial = untrack(() => found);
  const id = $derived(found.document.document_id);
  const draftKey = `${initial.document.document_id}:comment`;
  const draft = untrack(() => loadDraft(projectId, draftKey));
  function options() {
    try { return JSON.parse(draft?.retained || '{}') as Record<string, string>; }
    catch { return {}; }
  }
  const saved = options();
  let body = $state(draft?.text ?? '');
  let base = $state(draft?.base ?? initial.revision.number);
  let block = $state(draft?.block ?? initial.blocks[0]?.id ?? '');
  let kind = $state(saved.kind ?? 'block');
  let math = $state(saved.math ?? '');
  let quote = $state(saved.quote ?? '');
  let session = $state(saved.session ?? initial.document.session_id ?? '');
  let sessions = $state<Session[]>([]);
  let threads = $state<CommentThread[]>([]);
  let next = $state<string | null>(null);
  let busy = $state(false);
  let error = $state<unknown>(null);
  let persisted = $state(true);
  let loading = $state(false);
  const macros = $derived(macroSet(found.macros, `${projectId}:${found.macro_context?.key ?? 'legacy-document'}`));
  const anchorDocument = $derived(base === found.revision.number ? Promise.resolve(found) : api.document(projectId, id, base));
  $effect(() => {
    if (body) persisted = saveDraft(projectId, draftKey, { base, text: body, reason: '', block,
      retained: JSON.stringify({ kind, math, quote, session }) });
    else clearDraft(projectId, draftKey);
  });
  $effect(() => { void refresh(); api.project(projectId).then(project => sessions = project.sessions).catch(caught => error = caught); });
  async function refresh(after: string | null = null) {
    loading = true; error = null;
    try {
      const result = await api.comments(projectId, id, found.revision.number, after);
      threads = after === null ? result.threads : [...threads, ...result.threads]; next = result.next_after;
    } catch (caught) { error = caught; }
    finally { loading = false; }
  }
  async function create(anchored: DocumentRevision) {
    busy = true; error = null;
    try {
      const change = { action: 'create', anchor: commentAnchor(anchored, block, kind, math, quote), body,
        ...(session ? { addressed_to: session } : {}) };
      const request = await documentRequest(projectId, id, change);
      await api.changeComment(projectId, id, { ...change, operation_id: request.id });
      documentAnswered(request); body = ''; clearDraft(projectId, draftKey); await refresh();
    } catch (caught) { error = caught; }
    finally { busy = false; }
  }
  function discard() { body = ''; clearDraft(projectId, draftKey); }
</script>
<svelte:window onbeforeunload={(event) => { if (body && !persisted) event.preventDefault(); }} />
<section class="comments" aria-label="Document comments">
  <h2>Comments</h2>
  <p class="muted">Comments keep their original revision and quote. Address a comment to a session to queue a request for its main agent. An active run receives it at its next safe point; an idle session keeps it for its next manually started run.</p>
  <button disabled={loading || busy} onclick={() => refresh()}>Refresh comments</button>
  {#if error}<ErrorMessage {error} />{/if}
  {#if threads.length === 0 && !loading}<p class="muted">No comments on this revision.</p>{/if}
  {#each threads as thread (thread.thread_id)}
    <AnnotationThread {projectId} documentId={id} {thread} {sessions} {macros} changed={() => refresh()} />
  {/each}
  {#if next}<button disabled={loading} onclick={() => refresh(next)}>Load more threads</button>{/if}
  {#if found.matched && !found.revision.tombstone}
    <h3>Add a comment</h3>
    <p>Anchor revision {base}. {#if base !== found.revision.number}<button disabled={busy} onclick={() => { base = found.revision.number; block = found.blocks[0]?.id ?? ''; math = ''; quote = ''; }}>Use displayed revision for the anchor</button>{/if}</p>
    {#await anchorDocument}
      <p>Loading anchored revision…</p>
    {:then anchored}
      {@const selected = anchored.blocks.find(item => item.id === block)}
      <fieldset disabled={busy}>
        <label>Anchor block <select bind:value={block} onchange={() => { math = ''; quote = ''; }}>{#each anchored.blocks as item (item.id)}<option value={item.id}>{item.id} · {item.kind}</option>{/each}</select></label>
        <label>Anchor kind <select bind:value={kind}><option value="block">Whole block</option><option value="math">Formula</option><option value="range">Quoted text range</option></select></label>
        {#if kind === 'math'}<label>Formula <select bind:value={math}><option value="">Choose formula</option>{#each selected?.math ?? [] as span (span.id)}<option value={span.id}>{span.id} · {(span.tex ?? '').slice(0, 100)}</option>{/each}</select></label>{/if}
        {#if kind === 'range'}<label>Exact quoted text <input bind:value={quote} maxlength="4096" /></label>{/if}
        <details><summary>Selected block source</summary><pre>{selected?.source ?? ''}</pre></details>
        <label>Comment Markdown <textarea aria-label="Comment Markdown" bind:value={body} rows="5" maxlength="8192"></textarea></label>
        <label>Address to main agent <select bind:value={session}><option value="">No work request</option>{#each sessions as item (item.session_id)}<option value={item.session_id}>{item.session_id}{item.closed_at ? ' (closed; next run)' : ''}</option>{/each}</select></label>
        <button disabled={!body.trim()} onclick={() => create(anchored)}>Post comment</button>
        <button onclick={discard}>Discard comment draft</button>
      </fieldset>
    {:catch caught}<ErrorMessage error={caught} />{/await}
    {#if !persisted}<p role="alert">Browser storage is unavailable. Keep this tab open or copy your comment draft.</p>{/if}
    {#if body}<details><summary>Comment preview</summary><Markdown source={body} {macros} /></details>{/if}
  {/if}
</section>
<style>
  .comments { margin-top: 2rem; border-top: 1px solid var(--border, #9996); }
  label { display: block; margin: .6rem 0; }
  textarea { width: 100%; box-sizing: border-box; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 16rem; overflow: auto; }
  fieldset { border: 0; padding: 0; }
  button { margin: .25rem .5rem .25rem 0; }
</style>
