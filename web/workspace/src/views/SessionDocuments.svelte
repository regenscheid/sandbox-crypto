<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api, type DocumentInfo } from '../lib/api';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import SessionUploads from './SessionUploads.svelte';
  import ArtifactList from './ArtifactList.svelte';
  import DocumentView from './DocumentView.svelte';
  import PollStatus from './PollStatus.svelte';
  let { projectId, sessionId }: { projectId: string; sessionId: string } = $props();
  let view = $state<PollState<DocumentInfo[]>>(initialPoll());
  let selected = $state<string | null>(null);
  let opened = $state<string[]>([]);
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
  function open(id: string) {
    if (!opened.includes(id)) opened = [...opened, id];
    selected = id;
  }
</script>

<section aria-label="Session documents">
  <div hidden={selected !== null}>
    <h2>Documents & files</h2>
    <p class="muted">Your session workbook and shared project documents.</p>
    <PollStatus phase={view.phase} hasData={view.data !== null} what="Documents" />
    <div class="document-list">
      {#each documents as doc (doc.document_id)}
        <button class="document-card" onclick={() => open(doc.document_id)}>
          <span class="document-glyph" aria-hidden="true">≡</span>
          <span
            ><strong>{doc.title || doc.name}</strong><small
              >{doc.scope === 'project' ? 'Project' : 'Session'} · Revision {doc.head}</small
            ></span
          ><span aria-hidden="true">↗</span>
        </button>
      {:else}{#if view.data}<p class="empty-note">
            Documents created during your research will appear here.
          </p>{/if}{/each}
    </div>
    <SessionUploads {projectId} {sessionId} {open} />
    <ArtifactList {projectId} initial={{ session: sessionId, run: '', contains: '' }} />
  </div>
  {#if selected}<button
      class="back-link"
      onclick={() => {
        selected = null;
      }}>← All documents & files</button
    >{/if}
  {#each opened as id (id)}
    <div hidden={selected !== id} class="pane-document">
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
