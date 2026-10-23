<!-- SPDX-License-Identifier: MIT -->
<!-- The project's documents: a searchable library of the project's shared documents and
     each session's workbook, beside the one chosen, read as its document page reads it
     (DocumentView, compact). Titles are the agents' and the user's text, shown as text. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api, type DocumentInfo } from '../lib/api';
  import { browserState } from '../lib/browserState';
  import { clock } from '../lib/conversation';
  import { formatTime } from '../lib/format';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { documentHref } from '../lib/router';
  import { visible } from '../lib/text';
  import DocumentView from './DocumentView.svelte';
  import PollStatus from './PollStatus.svelte';

  let { projectId }: { projectId: string } = $props();
  const scope = untrack(() => `documents-page:${projectId}`);
  const kept = untrack(() => browserState.load(scope).draft ?? {});
  let selected = $state<string | null>(kept.selected || null);
  let query = $state(kept.query ?? '');
  let listOpen = $state(kept.list !== 'closed');
  let where = $state<'all' | 'project' | 'session'>(
    (['all', 'project', 'session'] as const).find((value) => value === kept.where) ?? 'all',
  );
  $effect(() => {
    browserState.save(scope, {
      selected: selected ?? '',
      query,
      where,
      list: listOpen ? 'open' : 'closed',
    });
  });
  let view = $state<PollState<DocumentInfo[]>>(initialPoll());
  $effect(() => {
    const project = projectId;
    return new Poller(
      (signal) => api.documents(project, signal),
      (value) => {
        view = value;
      },
      { intervalMs: 10000 },
    ).watch();
  });
  const shown = $derived(
    (view.data ?? [])
      .filter((item) => !item.deleted)
      .filter((item) => where === 'all' || item.scope === where)
      .filter((item) =>
        `${item.title} ${item.name}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
      )
      .sort((a, b) => b.created_at.localeCompare(a.created_at)),
  );
  const chosen = $derived(
    (view.data ?? []).find((item) => item.document_id === selected) ?? shown[0] ?? null,
  );
  const where_ = (item: DocumentInfo) =>
    item.scope === 'project' ? 'Project' : `Session ${visible(item.session_id ?? '')}`;
  const today = (iso: string) => new Date(iso).toDateString() === new Date().toDateString();
</script>

<div class="page-head">
  <div>
    <h1>Documents</h1>
    <p class="muted">The project's shared documents and each session's workbook</p>
  </div>
  <label class="sr-only" for="documents-search">Search documents</label>
  <input
    id="documents-search"
    type="search"
    placeholder="Search titles and names"
    bind:value={query}
  />
</div>
<div class="split" class:list-closed={!listOpen}>
  {#if !listOpen}
    <button
      type="button"
      class="library-rail"
      aria-label="Show the document list"
      title="Show the document list"
      onclick={() => (listOpen = true)}>Documents »</button
    >
  {/if}
  <section class="library" aria-label="Library" hidden={!listOpen}>
    <div class="library-head">
      <div class="segmented" role="tablist" aria-label="Scope">
        {#each [['all', 'All'], ['session', 'Workbooks'], ['project', 'Project']] as const as [value, label] (value)}
          <button
            type="button"
            role="tab"
            aria-selected={where === value}
            onclick={() => (where = value)}>{label}</button
          >
        {/each}
      </div>
      <button
        type="button"
        class="icon-button"
        aria-label="Hide the document list"
        title="Hide the document list"
        onclick={() => (listOpen = false)}>«</button
      >
    </div>
    <PollStatus phase={view.phase} hasData={view.data !== null} what="Documents" />
    {#each shown as item (item.document_id)}
      <button
        type="button"
        class="library-item"
        aria-current={chosen?.document_id === item.document_id ? 'true' : undefined}
        onclick={() => (selected = item.document_id)}
      >
        <strong>{visible(item.title || item.name)}</strong>
        <small
          >{where_(item)} · revision {item.head} · {today(item.created_at)
            ? clock(item.created_at)
            : formatTime(item.created_at)}</small
        >
      </button>
    {:else}
      {#if view.data}<p class="empty-note">
          {query ? 'No document matches.' : 'No documents yet.'}
        </p>{/if}
    {/each}
  </section>
  <article class="reader" aria-label="Document">
    {#if chosen}
      <div class="reader-bar">
        <a href={documentHref(projectId, chosen.document_id)}>Open the full page ↗</a>
      </div>
      {#key chosen.document_id}
        <DocumentView {projectId} documentId={chosen.document_id} revision={null} compact />
      {/key}
    {:else if view.data}
      <p class="empty-note">
        Agents write documents with the document tool, and you can import one with
        <code>sandbox-science-harness document import</code>.
      </p>
    {/if}
  </article>
</div>

<style>
  .split {
    display: flex;
    flex-wrap: wrap;
    min-height: 0;
    border-top: 1px solid var(--border);
    margin: 0 -32px;
  }
  .library-head {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-bottom: 4px;
  }
  .library-head .segmented {
    flex: 1;
  }
  .library-rail {
    writing-mode: vertical-rl;
    align-self: flex-start;
    min-height: 140px;
    margin: 12px 0 12px 12px;
    padding: 12px 6px;
    font-size: 12px;
    color: var(--muted);
  }
  .library {
    flex: 1 1 300px;
    max-width: 360px;
    min-width: 0;
    border-right: 1px solid var(--border);
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .library-item {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    min-height: 0;
    padding: 10px 12px;
    border: 0;
    border-radius: 8px;
    background: transparent;
    text-align: left;
  }
  .library-item strong {
    font-weight: 500;
    color: var(--text);
  }
  .library-item small {
    font-size: 12px;
    color: var(--muted);
  }
  .library-item[aria-current='true'] {
    background: var(--subtle);
  }
  .reader {
    flex: 999 1 480px;
    min-width: 0;
    padding: 0 24px 24px;
  }
  .reader-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 16px;
    align-items: center;
    justify-content: flex-end;
    padding: 8px 0 0;
    font-size: 13px;
  }
  @media (max-width: 760px) {
    .split {
      margin: 0 -16px;
    }
    .library {
      max-width: none;
      border-right: 0;
      border-bottom: 1px solid var(--border);
    }
  }
</style>
