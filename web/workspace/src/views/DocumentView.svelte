<!-- SPDX-License-Identifier: MIT -->
<!--
  A revision of a living document, read-only: its facts, its warnings, and its text
  rendered with the server's block and math IDs and its front matter's macros (living
  documents, LD-9, LD-26, LD-28). Where the browser's parse and the server's block map
  disagree, the view says so and leaves those parts without IDs.
-->
<script lang="ts">
  import { api, type DocumentRevision } from '../lib/api';
  import { guardedWrite } from '../lib/mutation';
  import type { Mismatch } from '../lib/attach';
  import { describeAuthor } from '../lib/authors';
  import { formatTime } from '../lib/format';
  import { anchorFromSelection, type ReaderAnchor } from '../lib/documentSelection';
  import { macroSet } from '../lib/macros';
  import Markdown from '../lib/Markdown.svelte';
  import { documentHref, historyHref, projectHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import DocumentEditor from './DocumentEditor.svelte';
  import HiddenBanner from './HiddenBanner.svelte';
  const commentComponent = import('./DocumentComments.svelte');

  let {
    projectId,
    documentId,
    revision,
    block = null,
    observedHead = null,
    compact = false,
  }: {
    projectId: string;
    documentId: string;
    revision: number | null;
    observedHead?: number | null;
    compact?: boolean;
    /** A block to bring into view and mark, as a finding's anchor names it. */
    block?: string | null;
  } = $props();
  /**
   * Bring the block into view and mark it. The text renders after the article mounts, so
   * the block is looked for again as it changes, until it is found.
   */
  function focusBlock(node: HTMLElement, wanted: string | null) {
    let id = wanted;
    const apply = (): boolean => {
      const found = id ? node.querySelector(`[data-block="${CSS.escape(id)}"]`) : null;
      for (const marked of node.querySelectorAll('.focused'))
        if (marked !== found) marked.classList.remove('focused');
      if (!found || found.classList.contains('focused')) return found !== null;
      found.classList.add('focused');
      found.scrollIntoView({ block: 'center' });
      return true;
    };
    const watcher = new MutationObserver(() => {
      if (apply()) watcher.disconnect();
    });
    if (!apply()) watcher.observe(node, { childList: true, subtree: true });
    return {
      update(next: string | null) {
        id = next;
        if (!apply()) watcher.observe(node, { childList: true, subtree: true });
      },
      destroy: () => watcher.disconnect(),
    };
  }
  let refresh = $state(0);
  const loading = $derived.by(() => {
    refresh;
    return api.document(projectId, documentId, revision);
  });
  let mismatches: Mismatch[] = $state([]);
  let inspector = $state<'comments' | 'edit'>('comments');
  let selectedAnchor = $state<ReaderAnchor | null>(null);
  let requestedAnchor = $state<ReaderAnchor | null>(null);
  let requestedEdit = $state<{ block: string } | null>(null);

  function watchSelection(node: HTMLElement, found: DocumentRevision) {
    let current = found;
    const handle = () => {
      selectedAnchor = anchorFromSelection(window.getSelection(), node, current);
    };
    node.addEventListener('mouseup', handle);
    node.addEventListener('keyup', handle);
    return {
      update(next: DocumentRevision) {
        if (
          next.document.document_id !== current.document.document_id ||
          next.revision.number !== current.revision.number
        )
          selectedAnchor = null;
        current = next;
      },
      destroy() {
        node.removeEventListener('mouseup', handle);
        node.removeEventListener('keyup', handle);
      },
    };
  }

  function commentOnSelection() {
    if (!selectedAnchor) return;
    requestedAnchor = { ...selectedAnchor };
    inspector = 'comments';
  }

  function editSelectedBlock() {
    if (!selectedAnchor) return;
    requestedEdit = { block: selectedAnchor.block };
    inspector = 'edit';
  }

  /** Move a reference's pin to a newer revision of its file (figures, FG-8): a patch of
   * the current revision, whose text is unchanged; the document is read again after it. */
  async function repin(found: DocumentRevision, block: string, path: string, to: number) {
    const body = {
      action: 'patch',
      base: found.document.head,
      reason: `Move the pin of ${path} to revision ${to}`,
      operations: [{ op: 'repin', block, path, revision: to }],
    };
    const scope = `${projectId}:${found.document.document_id}:${block}:${path}:${to}`;
    await guardedWrite('document-repin', scope, body, (change) =>
      api.changeDocument(projectId, found.document.document_id, change),
    );
    refresh += 1;
  }

  function lines(found: Mismatch): string {
    const [first, end] = found.lines;
    return end - first <= 1 ? `line ${first + 1}` : `lines ${first + 1}–${end}`;
  }
</script>

{#if !compact}<nav class="trail">
    <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> / documents /
  </nav>{/if}

{#await loading}
  <p class="muted">Loading…</p>
{:then found}
  {@const info = found.document}
  {@const shown = found.revision}
  {@const macros = macroSet(
    found.macros,
    `${projectId}:${found.macro_context?.key ?? 'legacy-document'}`,
  )}
  {#if found.macro_context?.problems?.length}<p role="status">
      {found.macro_context.problems.join('; ')}
    </p>{/if}
  <!-- A first revision added every block: marking them all would say nothing. -->
  {@const blocks =
    shown.parent === null
      ? found.blocks.map((block) => ({ ...block, status: 'kept' }))
      : found.blocks}
  {@const input = { blocks, macros }}
  <div class="document-heading">
    <div>
      <span class="document-eyebrow"
        >{info.scope === 'project' ? 'Project document' : 'Session document'} · Revision {shown.number}</span
      >
      <h1>{found.title ?? info.title}</h1>
    </div>
    <div class="document-heading-actions">
      <button
        class:active={inspector === 'comments'}
        aria-pressed={inspector === 'comments'}
        onclick={() => (inspector = 'comments')}>Comments</button
      >
      <button
        class:active={inspector === 'edit'}
        aria-pressed={inspector === 'edit'}
        onclick={() => (inspector = 'edit')}>Edit</button
      >
      <a href={historyHref(projectId, info.document_id)}>History</a>
    </div>
  </div>
  {#if revision === null && observedHead !== null && observedHead > shown.number}
    <p role="status">
      A newer revision is available. Your unsaved draft will be kept. <button
        onclick={() => {
          refresh += 1;
        }}>Read latest revision</button
      >
    </p>
  {/if}
  <details class="document-details">
    <summary>Revision {shown.number} · {describeAuthor(shown.author)} · Details & history</summary>
    <dl class="facts">
      <dt>Document</dt>
      <dd>
        <code>{info.name}</code>
        <span class="muted">
          {info.scope === 'project' ? "the project's" : `in session ${info.session_id}'s workbook`},
          {info.document_id}
        </span>
      </dd>
      <dt>Revision</dt>
      <dd>
        {shown.number} of {info.head}
        {#if shown.number > 1}
          · <a href={documentHref(projectId, info.document_id, shown.number - 1)}>previous</a>
        {/if}
        {#if shown.number < info.head}
          · <a href={documentHref(projectId, info.document_id, shown.number + 1)}>next</a>
          · <a href={documentHref(projectId, info.document_id)}>current</a>
        {/if}
        · <a href={historyHref(projectId, info.document_id)}>history</a>
      </dd>
      <dt>Written by</dt>
      <dd>
        {describeAuthor(shown.author)}
        {#if shown.accepted_by}
          <span class="muted">(a suggestion, accepted by {describeAuthor(shown.accepted_by)})</span>
        {/if}
      </dd>
      <dt>When</dt>
      <dd>{formatTime(shown.created_at)}</dd>
      <dt>Reason</dt>
      <dd>{shown.reason}</dd>
      {#if shown.normalized.converted && Object.keys(shown.normalized.converted).length > 0}
        <dt>Delimiters</dt>
        <dd>saved in the dollar form (LD-42)</dd>
      {/if}
    </dl>
  </details>

  <div class="document-shell">
    <div class="document-workspace" class:compact>
      <div class="document-reading">
        {#if shown.warnings.length > 0}
          <h2>Warnings</h2>
          <ul class="warnings">
            {#each shown.warnings as warning, index (index)}
              <li>
                <code>{warning.kind}</code>
                {#if warning.block}<span class="muted"
                    >in {warning.block}{warning.math ? `, ${warning.math}` : ''}</span
                  >{/if}:
                {warning.message}
              </li>
            {/each}
          </ul>
        {/if}

        {#if macros.problems.length > 0}
          <p class="problem">
            Some of the front matter's macros are not used: {macros.problems.join('; ')}.
          </p>
        {/if}

        {#if !found.matched}
          <p class="problem">
            The server could not parse this revision as its block map was made, with a parser of
            another version, so its formulas are shown without their IDs.
          </p>
        {/if}

        {#if mismatches.length > 0}
          <div class="mismatch" role="note">
            <p>
              The browser reads this revision differently from the server in {mismatches.length}
              {mismatches.length === 1 ? 'place' : 'places'} (LD-9). Those parts are shown without their
              IDs, outlined:
            </p>
            <ul>
              {#each mismatches as mismatch, index (index)}
                <li>{lines(mismatch)}: {mismatch.message}</li>
              {/each}
            </ul>
          </div>
        {/if}

        {#if shown.tombstone}
          <p>The document was deleted in this revision. Its earlier revisions remain.</p>
        {:else}
          <!-- Only a block of this revision is looked for; another is said to be missing. -->
          {@const known = block !== null && found.blocks.some((item) => item.id === block)}
          {#if block !== null && !known}<p role="status">
              Block {block} is not in this revision; the link may name an earlier one.
            </p>{/if}
          <!-- Only when the document hides something from its rendering (threat model, A3). -->
          <HiddenBanner text={found.text} quiet />
          <div class="selection-action" role="status">
            <span
              >{selectedAnchor
                ? selectedAnchor.kind === 'range'
                  ? `Selected: “${selectedAnchor.quote.slice(0, 90)}${selectedAnchor.quote.length > 90 ? '…' : ''}”`
                  : selectedAnchor.kind === 'math'
                    ? `Selected formula ${selectedAnchor.math}`
                    : `Selected block ${selectedAnchor.block}`
                : 'Select a passage or formula to work on it.'}</span
            >
            <div class="selection-buttons">
              <button disabled={!selectedAnchor} onclick={commentOnSelection}>Comment</button>
              <button disabled={!selectedAnchor} onclick={editSelectedBlock}>Edit block</button>
            </div>
          </div>
          <article
            class="document"
            use:focusBlock={known ? block : null}
            use:watchSelection={found}
          >
            <Markdown
              source={found.text}
              document={input}
              bind:mismatches
              pins={found.pins ?? []}
              checks={found.figure_checks ?? []}
              {projectId}
              repin={shown.number === info.head ? (b, p, r) => repin(found, b, p, r) : null}
            />
          </article>
        {/if}
      </div>
      <aside class="document-inspector" aria-label="Document tools">
        <div class="document-inspector-tabs" role="group" aria-label="Document tools">
          <button aria-pressed={inspector === 'comments'} onclick={() => (inspector = 'comments')}
            >Comments</button
          >
          <button aria-pressed={inspector === 'edit'} onclick={() => (inspector = 'edit')}
            >Edit source</button
          >
        </div>
        <div hidden={inspector !== 'edit'}>
          {#key `${info.document_id}:${shown.number}`}
            <DocumentEditor
              {projectId}
              {found}
              {compact}
              {requestedEdit}
              refreshed={() => (refresh += 1)}
            />
          {/key}
        </div>
        <div hidden={inspector !== 'comments'}>
          {#key `${info.document_id}:${shown.number}`}
            {#await commentComponent then component}
              {@const Comments = component.default}
              <Comments
                {projectId}
                {found}
                {requestedAnchor}
                consumeAnchor={() => (requestedAnchor = null)}
              />
            {:catch error}<ErrorMessage {error} />{/await}
          {/key}
        </div>
      </aside>
    </div>
  </div>
{:catch error}
  <ErrorMessage {error} />
{/await}

<style>
  .document-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin: 0.75rem 0;
  }
  .document-heading h1 {
    margin: 0.1rem 0;
    font-size: 1.3rem;
  }
  .document-eyebrow {
    color: var(--muted);
    font-size: 0.78rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .document-heading-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem;
  }
  .document-heading-actions a {
    margin-left: 0.4rem;
    font-size: 0.9rem;
  }
  .document-heading-actions button.active {
    border-color: var(--link);
    color: var(--link);
  }
  .document-details {
    margin-bottom: 1rem;
    color: var(--muted);
    font-size: 0.85rem;
  }
  .document-shell {
    container-type: inline-size;
    container-name: document-shell;
  }
  .document-workspace {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(19rem, 36%);
    gap: 1rem;
    align-items: start;
  }
  .document-reading {
    min-width: 0;
  }
  .document-inspector {
    min-width: 0;
    padding: 1rem;
    border: 1px solid var(--border);
    border-radius: 0.6rem;
    background: var(--panel);
    position: sticky;
    top: 0.5rem;
    max-height: calc(100vh - 5rem);
    overflow: auto;
  }
  .document-inspector-tabs {
    display: flex;
    gap: 0.25rem;
    margin-bottom: 1rem;
    border-bottom: 1px solid var(--border);
  }
  .document-inspector-tabs button {
    border: 0;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    background: transparent;
    padding: 0.5rem 0.75rem;
  }
  .document-inspector-tabs button[aria-pressed='true'] {
    border-bottom-color: var(--link);
    color: var(--link);
    font-weight: 600;
  }
  .selection-action {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 0.6rem;
    padding: 0.6rem 0.75rem;
    border: 1px solid var(--link);
    border-radius: 0.5rem;
    background: var(--panel);
    font-size: 0.85rem;
  }
  .selection-action span {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .selection-action button {
    flex: none;
  }
  .selection-buttons {
    display: flex;
    gap: 0.3rem;
  }
  @container document-shell (max-width: 790px) {
    .document-workspace {
      grid-template-columns: minmax(0, 1fr);
    }
    .document-inspector {
      position: static;
      max-height: none;
    }
  }
  @media (max-width: 700px) {
    .document-heading {
      flex-direction: column;
    }
  }
</style>
