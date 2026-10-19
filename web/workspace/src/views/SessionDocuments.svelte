<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { tick } from 'svelte';
  import { api, type DocumentInfo } from '../lib/api';
  import { formatTime } from '../lib/format';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import SessionUploads from './SessionUploads.svelte';
  import ArtifactList from './ArtifactList.svelte';
  import DocumentView from './DocumentView.svelte';
  import PollStatus from './PollStatus.svelte';
  let { projectId, sessionId }: { projectId: string; sessionId: string } = $props();
  let view = $state<PollState<DocumentInfo[]>>(initialPoll());
  let selected = $state<string | null>(null);
  let opened = $state<string[]>([]);
  // Closing a tab hides its editor, retaining even drafts that could not reach browser storage.
  let visited = $state<string[]>([]);
  let titles = $state<Record<string, string>>({});
  let query = $state('');
  let kind = $state('documents');
  let scope = $state('all');
  let sort = $state('newest');
  $effect(() => {
    const project = projectId;
    const poller = new Poller(
      (signal) => api.documents(project, signal),
      (value) => {
        view = value;
      },
      { intervalMs: 5000 },
    );
    return poller.watch();
  });
  const documents = $derived(
    (view.data ?? []).filter((doc) => doc.scope === 'project' || doc.session_id === sessionId),
  );
  const shown = $derived(
    documents
      .filter(
        (doc) =>
          (scope === 'all' || doc.scope === scope) &&
          query
            .toLowerCase()
            .trim()
            .split(/\s+/)
            .every((word) => `${doc.title} ${doc.name}`.toLowerCase().includes(word)),
      )
      .sort((a, b) =>
        sort === 'name'
          ? (a.title || a.name).localeCompare(b.title || b.name)
          : b.created_at.localeCompare(a.created_at) || a.name.localeCompare(b.name),
      ),
  );
  const label = (id: string) => {
    const doc = documents.find((doc) => doc.document_id === id);
    return doc?.title || doc?.name || titles[id] || 'Document';
  };
  async function focusSelected() {
    await tick();
    document.getElementById(`document-tab-${selected ?? 'library'}`)?.focus();
  }
  function open(id: string) {
    titles[id] = label(id);
    if (!opened.includes(id)) opened = [...opened, id];
    if (!visited.includes(id)) visited = [...visited, id];
    selected = id;
    void focusSelected();
  }
  function close(id: string) {
    const index = opened.indexOf(id);
    opened = opened.filter((value) => value !== id);
    if (selected === id) selected = opened[Math.min(index, opened.length - 1)] ?? null;
    void focusSelected();
  }
  function moveTab(event: KeyboardEvent, id: string | null) {
    const tabs = [null, ...opened];
    const index = tabs.indexOf(id);
    if (event.key === 'Delete' && id) {
      event.preventDefault();
      close(id);
      return;
    }
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? tabs.length - 1
          : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    selected = tabs[next]!;
    void focusSelected();
  }
</script>

<section class="session-documents" aria-label="Session documents">
  <div class="document-tabs" role="tablist" aria-label="Open documents">
    <button
      role="tab"
      id="document-tab-library"
      aria-controls="document-library"
      aria-selected={selected === null}
      tabindex={selected === null ? 0 : -1}
      onclick={() => (selected = null)}
      onkeydown={(event) => moveTab(event, null)}>All documents</button
    >
    {#each opened as id (id)}
      <div class="document-tab" class:active={selected === id} role="presentation">
        <button
          role="tab"
          id={`document-tab-${id}`}
          aria-controls={`document-panel-${id}`}
          aria-selected={selected === id}
          tabindex={selected === id ? 0 : -1}
          title={label(id)}
          onclick={() => (selected = id)}
          onkeydown={(event) => moveTab(event, id)}>{label(id)}</button
        >
        <button
          class="document-tab-close"
          aria-label={`Close ${label(id)}`}
          title="Close tab; draft is retained"
          onclick={() => close(id)}>×</button
        >
      </div>
    {/each}
  </div>
  <div
    id="document-library"
    role="tabpanel"
    aria-labelledby="document-tab-library"
    tabindex="0"
    hidden={selected !== null}
  >
    <div class="document-filters">
      <label class="document-search"
        >Search
        <input
          type="search"
          bind:value={query}
          placeholder={kind === 'artifacts'
            ? 'Search artifact paths…'
            : 'Search titles and filenames…'}
        />
      </label>
      <div class="document-filter-row">
        <label
          >Show <select bind:value={kind}
            ><option value="documents">Documents</option><option value="uploads"
              >Uploaded files</option
            ><option value="artifacts">Research artifacts</option></select
          ></label
        >
        {#if kind === 'documents'}
          <label
            >Scope <select bind:value={scope}
              ><option value="all">Session & project</option><option value="session"
                >This session</option
              ><option value="project">Shared project</option></select
            ></label
          >
        {/if}
        {#if kind !== 'artifacts'}<label
            >Sort <select bind:value={sort}
              ><option value="newest">Newest created</option><option value="name">Name A–Z</option
              ></select
            ></label
          >{/if}
      </div>
    </div>
    <div hidden={kind !== 'documents'}>
      <PollStatus phase={view.phase} hasData={view.data !== null} what="Documents" />
      {#if view.data}<p class="document-count" role="status">
          {shown.length} of {documents.length} documents
        </p>{/if}
      <ul class="document-list" aria-label="Documents">
        {#each shown as doc (doc.document_id)}
          <li>
            <button class="document-card" onclick={() => open(doc.document_id)}>
              <span class="document-glyph" aria-hidden="true">≡</span>
              <span
                ><strong>{doc.title || doc.name}</strong><small
                  >{doc.scope === 'project' ? 'Shared project' : 'This session'} · Revision {doc.head}{doc.deleted
                    ? ' · Deleted'
                    : ''}</small
                ></span
              >
              <time datetime={doc.created_at} title={formatTime(doc.created_at)}
                >{new Date(doc.created_at).toLocaleDateString(undefined, {
                  month: 'short',
                  day: 'numeric',
                })}</time
              >
            </button>
          </li>
        {:else}{#if view.data}<li class="empty-note">
              {documents.length
                ? 'No documents match these filters.'
                : 'Documents created during your research will appear here.'}
            </li>{/if}{/each}
      </ul>
    </div>
    <div hidden={kind !== 'uploads'}>
      <SessionUploads {projectId} {sessionId} {open} {query} {sort} />
    </div>
    {#if kind === 'artifacts'}<ArtifactList
        {projectId}
        externalFilter={{ session: sessionId, run: '', contains: query }}
      />{/if}
  </div>
  {#each visited as id (id)}
    <div
      hidden={selected !== id}
      class="pane-document"
      id={`document-panel-${id}`}
      role="tabpanel"
      aria-labelledby={opened.includes(id) ? `document-tab-${id}` : undefined}
      tabindex="0"
    >
      <DocumentView
        compact
        {projectId}
        documentId={id}
        revision={null}
        observedHead={documents.find((doc) => doc.document_id === id)?.head ?? null}
      />
    </div>
  {/each}
</section>
