<!-- SPDX-License-Identifier: MIT -->
<!--
  Who changed what in a living document (LD-11), and what each revision looked like: the
  revisions newest first, each with who wrote it, why and how much it changed; the chosen
  one's changes from another revision, passage by passage under its headings, or the
  revision itself as the document page draws it; and the patches agents submitted, which
  the user may review.
-->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, type DocumentHistory, type DocumentRevision } from '../lib/api';
  import { textDiff } from '../lib/approval';
  import { authorName, describeAuthor } from '../lib/authors';
  import { dayAndTime } from '../lib/conversation';
  import { Detail, noDetail } from '../lib/detail';
  import {
    compareRevisions,
    newestFirst,
    patchState,
    reviewable,
    summarizeChanges,
  } from '../lib/documentHistory';
  import { formatTime } from '../lib/format';
  import { macroSet } from '../lib/macros';
  import Markdown from '../lib/Markdown.svelte';
  import { visible } from '../lib/text';
  import { documentHref, documentsHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import PatchReview from './PatchReview.svelte';

  let { projectId, documentId }: { projectId: string; documentId: string } = $props();
  const shown = $state(noDetail<string, DocumentHistory>());
  // Keyed by the document alone: a document ID is unique in the whole store (the documents
  // table's key), and the read names the project as the view's properties do now.
  const histories = new Detail(shown, (document) => api.history(projectId, document));
  $effect(() => {
    void histories.show(documentId);
  });

  /** The revision shown, and the one it is compared with; null until the history is read. */
  let selected = $state<number | null>(null);
  let against = $state<number | null>(null);
  let mode = $state<'changes' | 'document'>('changes');
  $effect(() => {
    const history = shown.value;
    if (history && selected === null) untrack(() => choose(history, history.document.head));
  });
  function choose(history: DocumentHistory, number: number) {
    selected = number;
    const revision = history.revisions.find((item) => item.number === number);
    against = revision?.parent ?? null;
  }
  // Each revision is read once; they never change.
  const read = new Map<number, Promise<DocumentRevision>>();
  function revision(number: number): Promise<DocumentRevision> {
    let found = read.get(number);
    if (!found) {
      found = api.document(projectId, documentId, number);
      read.set(number, found);
    }
    return found;
  }
  const pair = $derived(
    selected === null
      ? null
      : Promise.all([revision(selected), against === null ? null : revision(against)]),
  );
  const passages = (count: number) => `${count} unchanged ${count === 1 ? 'passage' : 'passages'}`;
</script>

{#if shown.error}
  <ErrorMessage error={shown.error} />
{:else if !shown.value}
  <p class="muted">Loading…</p>
{:else}
  {@const history = shown.value}
  {@const info = history.document}
  {@const revisions = newestFirst(history.revisions)}
  {@const chosen = history.revisions.find((item) => item.number === selected)}
  <div class="page-head">
    <div>
      <h1>History</h1>
      <p class="muted">
        Every revision of <a href={documentHref(projectId, info.document_id)}>{info.title}</a>
      </p>
    </div>
    <a class="button-link" href={documentsHref(projectId)}>← Documents</a>
  </div>

  <div class="history-shell">
    <div class="history">
      <ol class="timeline" aria-label="Revisions">
        {#each revisions as item (item.number)}
          <li>
            <button
              type="button"
              aria-current={item.number === selected ? 'true' : undefined}
              onclick={() => choose(history, item.number)}
            >
              <span class="timeline-head"
                ><strong>Revision {item.number}</strong>{#if item.number === info.head}<span
                    class="pill">Current</span
                  >{/if}<span class="muted when" title={formatTime(item.created_at)}
                  >{dayAndTime(item.created_at)}</span
                ></span
              >
              <span class="who" title={describeAuthor(item.author)}
                >{authorName(item.author)}{item.accepted_by
                  ? `, accepted by ${authorName(item.accepted_by).toLowerCase()}`
                  : ''}</span
              >
              {#if item.reason}<span class="reason">{visible(item.reason)}</span>{/if}
              <span class="muted summary">{summarizeChanges(item)}</span>
            </button>
          </li>
        {/each}
      </ol>

      <section class="revision" aria-label="The chosen revision">
        {#if chosen && selected !== null}
          <div class="revision-bar">
            <div class="segmented" role="tablist" aria-label="Show">
              <button
                type="button"
                role="tab"
                aria-selected={mode === 'changes'}
                onclick={() => (mode = 'changes')}>Changes</button
              ><button
                type="button"
                role="tab"
                aria-selected={mode === 'document'}
                onclick={() => (mode = 'document')}>Document</button
              >
            </div>
            {#if mode === 'changes'}
              <label class="against"
                >Compared with <select
                  value={against === null ? '' : String(against)}
                  onchange={(event) => {
                    const value = event.currentTarget.value;
                    against = value === '' ? null : Number(value);
                  }}
                >
                  {#each revisions as item (item.number)}
                    {#if item.number !== selected}<option value={String(item.number)}
                        >revision {item.number}{item.number === chosen.parent
                          ? ' (the one before)'
                          : item.number === info.head
                            ? ' (current)'
                            : ''}</option
                      >{/if}
                  {/each}
                  {#if chosen.parent === null}<option value="">nothing (the first revision)</option
                    >{/if}
                </select></label
              >
            {/if}
            <a class="button-link" href={documentHref(projectId, info.document_id, selected)}
              >{selected === info.head ? 'Open' : 'Open to restore'}</a
            >
          </div>
          {#await pair}
            <p class="muted">Loading revision {selected}…</p>
          {:then loaded}
            {#if loaded}
              {@const [after, before] = loaded}
              {#if mode === 'document'}
                {@const blocks =
                  after.revision.parent === null
                    ? after.blocks.map((block) => ({ ...block, status: 'kept' }))
                    : after.blocks}
                <p class="muted note">
                  {after.revision.parent === null
                    ? 'The first revision, as it was written.'
                    : `Marked in the margin: what revision ${selected} changed from the one before.`}
                </p>
                <article class="document">
                  <Markdown
                    source={after.text}
                    document={{
                      blocks,
                      macros: macroSet(
                        after.macros ?? {},
                        `${projectId}:${after.macro_context?.key ?? 'legacy-document'}`,
                      ),
                    }}
                    pins={after.pins ?? []}
                    {projectId}
                  />
                </article>
              {:else if before === null}
                <p class="muted note">
                  The first revision: nothing came before it. Choose Document to read it.
                </p>
              {:else}
                {@const changes = compareRevisions(
                  before,
                  after,
                  before.revision.number === after.revision.parent ? chosen.moved : [],
                )}
                {#if changes === null}
                  {@const whole = textDiff(before.text, after.text)}
                  {#if whole === null}<p class="muted">The texts are too long to compare here.</p>
                  {:else}<pre
                      class="diff">{#each whole as part, index (index)}{#if part.kind === 'removed'}<del
                            >{visible(part.text)}</del
                          >{:else if part.kind === 'added'}<ins>{visible(part.text)}</ins
                          >{:else}{visible(part.text)}{/if}{/each}</pre>{/if}
                {:else if changes.every((item) => item.kind === 'same')}
                  <p class="muted note">
                    The text is the same in revisions {before.revision.number} and {selected}.
                  </p>
                {:else}
                  <ol class="changes">
                    {#each changes as change, index (index)}
                      {#if change.kind === 'same'}
                        <li class="same muted">{passages(change.count)}</li>
                      {:else}
                        <li class="change {change.kind}">
                          <div class="change-head">
                            <span class="pill {change.kind}"
                              >{{
                                added: 'Added',
                                removed: 'Removed',
                                changed: 'Edited',
                                moved: 'Moved',
                              }[change.kind]}</span
                            ><span class="muted"
                              >{change.passage === 'heading'
                                ? 'heading'
                                : change.passage}{change.section && change.passage !== 'heading'
                                ? ` in “${visible(change.section)}”`
                                : ''}</span
                            >
                          </div>
                          {#if change.kind === 'changed' && change.diff}<pre
                              class="diff">{#each change.diff as part, n (n)}{#if part.kind === 'removed'}<del
                                    >{visible(part.text)}</del
                                  >{:else if part.kind === 'added'}<ins>{visible(part.text)}</ins
                                  >{:else}{visible(part.text)}{/if}{/each}</pre>
                          {:else if change.kind === 'changed'}<pre class="diff"><del
                                >{visible(change.before)}</del
                              ><ins>{visible(change.after)}</ins></pre>
                          {:else if change.kind === 'removed'}<pre class="diff"><del
                                >{visible(change.before)}</del
                              ></pre>
                          {:else}<pre class="diff">{#if change.kind === 'added'}<ins
                                  >{visible(change.after)}</ins
                                >{:else}{visible(change.after)}{/if}</pre>{/if}
                        </li>
                      {/if}
                    {/each}
                  </ol>
                {/if}
              {/if}
            {/if}
          {:catch error}<ErrorMessage {error} />{/await}
        {/if}
      </section>
    </div>
  </div>

  <!-- A new head (an applied patch, or any other change) closes every open preview. -->
  {#key history.document.head}
    {#if history.patches.length > 0}
      <section class="patches" aria-label="Suggestions and patches">
        <h2>Suggestions and patches</h2>
        {#each newestFirst(history.patches) as patch (patch.patch_id)}
          <article class="patch">
            <div class="patch-head">
              <strong title={describeAuthor(patch.author)}>{authorName(patch.author)}</strong>
              <span class="muted">{formatTime(patch.created_at)} · on revision {patch.base}</span>
              <span class="pill" class:waiting={patch.state === 'suggested'}
                >{patchState(patch.state)}</span
              >
              {#if patch.revision}<a
                  href={documentHref(projectId, info.document_id, patch.revision)}
                  >became revision {patch.revision}</a
                >{/if}
            </div>
            {#if patch.reason}<p>{visible(patch.reason)}</p>{/if}
            {#if reviewable(patch)}
              <PatchReview
                {projectId}
                documentId={info.document_id}
                patchId={patch.patch_id}
                suggested={patch.state === 'suggested'}
                refreshed={() => void histories.reload()}
              />
            {/if}
          </article>
        {/each}
      </section>
    {/if}
  {/key}
{/if}

<style>
  .history-shell {
    container-type: inline-size;
  }
  .history {
    display: grid;
    grid-template-columns: minmax(16rem, 22rem) minmax(0, 1fr);
    gap: 24px;
    align-items: start;
  }
  .timeline {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
    position: sticky;
    top: 0.5rem;
    max-height: calc(100vh - 8rem);
    overflow: auto;
  }
  .timeline button {
    width: 100%;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 2px;
    padding: 10px 12px;
    border: 1px solid transparent;
    border-radius: 10px;
    background: transparent;
    text-align: left;
    color: var(--text);
    font: inherit;
  }
  .timeline button:hover {
    background: var(--subtle);
  }
  .timeline button[aria-current='true'] {
    background: var(--accent-soft);
    border-color: var(--accent-line);
  }
  .timeline-head {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .when {
    margin-left: auto;
    font-size: 12px;
  }
  .who,
  .summary {
    font-size: 12px;
  }
  .who {
    color: var(--body-text);
  }
  .reason {
    font-size: 13px;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .pill {
    padding: 0 8px;
    border-radius: 9px;
    background: var(--subtle);
    color: var(--body-text);
    font-size: 12px;
    font-weight: 500;
  }
  .pill.added {
    background: var(--ok-soft);
    color: var(--ok);
  }
  .pill.removed {
    background: var(--danger-soft);
    color: var(--failed);
  }
  .pill.changed,
  .pill.waiting {
    background: var(--warn-soft);
    color: var(--warn-text);
  }
  .pill.moved {
    background: var(--accent-soft);
    color: var(--link);
  }
  .revision {
    min-width: 0;
  }
  .revision-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px 14px;
    padding-bottom: 12px;
    margin-bottom: 12px;
    border-bottom: 1px solid var(--border);
  }
  .against {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--muted);
  }
  .revision-bar .button-link {
    margin-left: auto;
  }
  .note {
    margin: 0 0 12px;
  }
  .changes {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .same {
    font-size: 12px;
    padding-left: 12px;
  }
  .change {
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 12px;
  }
  .change-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    font-size: 12px;
  }
  .diff {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-family: ui-monospace, Menlo, monospace;
    font-size: 13px;
    line-height: 1.6;
    background: transparent;
  }
  .diff ins {
    background: var(--ok-soft);
    color: var(--ok);
    text-decoration: none;
  }
  .diff del {
    background: var(--danger-soft);
    color: var(--failed);
  }
  .patches {
    margin-top: 32px;
  }
  .patch {
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 12px;
    margin-bottom: 10px;
  }
  .patch-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .patch p {
    margin: 6px 0;
  }
  @container (max-width: 760px) {
    .history {
      grid-template-columns: minmax(0, 1fr);
    }
    .timeline {
      position: static;
      max-height: 18rem;
    }
  }
</style>
