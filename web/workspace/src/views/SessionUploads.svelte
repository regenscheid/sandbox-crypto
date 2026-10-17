<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { uploadsApi, type UploadPage, type UserUpload } from '../lib/uploads';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import PollStatus from './PollStatus.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  let {
    projectId,
    sessionId,
    open,
    query = '',
    sort = 'newest',
  }: {
    projectId: string;
    sessionId: string;
    open: (id: string) => void;
    query?: string;
    sort?: string;
  } = $props();
  let view = $state<PollState<UploadPage>>(initialPoll());
  let earlier = $state<UserUpload[]>([]);
  let after = $state(0);
  let error = $state<unknown>(null);
  let busy = $state(false);
  const files = $derived(
    [...earlier, ...(view.data?.uploads ?? [])]
      .filter((file) =>
        query
          .toLowerCase()
          .trim()
          .split(/\s+/)
          .every((word) => file.filename.toLowerCase().includes(word)),
      )
      .sort((a, b) =>
        sort === 'name' ? a.filename.localeCompare(b.filename) : b.number - a.number,
      ),
  );
  $effect(() => {
    const project = projectId,
      session = sessionId,
      cursor = after;
    const poller = new Poller(
      (signal) => uploadsApi.list(project, session, cursor, signal),
      (value) => {
        view = value;
      },
      { intervalMs: 5000 },
    );
    return poller.watch();
  });
  async function edit(file: UserUpload) {
    busy = true;
    error = null;
    try {
      open((await uploadsApi.document(file)).document_id);
    } catch (caught) {
      error = caught;
    } finally {
      busy = false;
    }
  }
</script>

<h3>Uploaded files</h3>
<PollStatus phase={view.phase} hasData={view.data !== null} what="Uploaded files" />
<ul class="upload-list">
  {#each files as file (file.upload_id)}<li>
      <a href={uploadsApi.download(file)} download>{file.filename}</a><small
        >{(file.size / 1024).toFixed(1)} KiB · Original</small
      >
      {#if /\.(md|markdown|txt|tex)$/i.test(file.filename) && file.size <= 1024 * 1024}<button
          disabled={busy}
          onclick={() => edit(file)}>Open as document</button
        >{/if}
    </li>{:else}{#if view.data}<li class="muted">
        {query.trim()
          ? 'No loaded uploads match this search.'
          : 'Attach files in chat to add them here.'}
      </li>{/if}{/each}
</ul>
{#if view.data?.next_after}<p class="muted">
    Search and sort apply to loaded uploads. Load more to include older files.
  </p>
  <button
    onclick={() => {
      earlier = [...earlier, ...view.data!.uploads];
      after = view.data!.next_after!;
      view = initialPoll();
    }}>More uploads</button
  >{/if}
{#if error}<ErrorMessage {error} />{/if}
