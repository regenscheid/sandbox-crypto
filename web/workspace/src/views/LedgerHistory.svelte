<!-- SPDX-License-Identifier: MIT -->
<!-- An entry's revisions (docs/specs/ledger.md, LG-34): read back a page at a time on
     request, each with its status, whether its statement changed, who made it, when and
     why, and where it came from when it was imported; each leads to that revision. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { EntryRevision } from '../lib/api-types';
  import { Earlier } from '../lib/earlier';
  import { formatTime } from '../lib/format';
  import { authorText, provenanceText, statusText } from '../lib/ledger';
  import { emptyList } from '../lib/pagedList';
  import { ledgerEntryHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    entryId,
    handle,
    shown,
  }: { projectId: string; entryId: string; handle: string; shown: number } = $props();
  const pages = $state(emptyList<EntryRevision[], number>());
  // The entry view is keyed by the entry and revision it shows, so these never change here.
  const history = new Earlier(pages, async (before) => {
    const found = await api.ledgerHistory(projectId, entryId, before);
    return { items: [found.history.revisions], next: found.history.next_before };
  });
  // Newest first, as the server reads them back: each page newest first, and each page
  // older than the one before it.
  const revisions = $derived(pages.items.flat());
</script>

<h2>History</h2>
<button
  type="button"
  disabled={pages.loading || !history.available()}
  onclick={() => history.read()}
  >{history.started ? 'Load earlier revisions' : 'Read its revisions'}</button
>
{#if revisions.length}
  <ul class="history">
    {#each revisions as revision (revision.revision)}
      <li>
        {#if revision.revision === shown}<strong>Revision {revision.revision}</strong> (shown){:else}<a
            href={ledgerEntryHref(projectId, handle, revision.revision)}
            >Revision {revision.revision}</a
          >{/if}: {statusText(revision.status)}{revision.statement_changed
          ? ', statement changed'
          : ''}. {revision.title}
        <p class="muted">
          {formatTime(revision.created)} by {authorText(revision.author)}: {revision.reason}
          {#if revision.provenance}<br />{provenanceText(revision.provenance)}{/if}
        </p>
      </li>
    {/each}
  </ul>
{/if}
{#if pages.error}<ErrorMessage error={pages.error} />{/if}
