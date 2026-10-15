<!-- SPDX-License-Identifier: MIT -->
<!-- One ledger entry at a revision (docs/specs/ledger.md, LG-1, LG-5): its statement, with
     its math, its own fields, the links it holds and those other entries hold to it, each
     leading where it points. Read-only. -->
<script lang="ts">
  import { api } from '../lib/api';
  import Markdown from '../lib/Markdown.svelte';
  import { formatTime } from '../lib/format';
  import { authorText, fieldRows, linksByKind, statusText, TYPE_NAME } from '../lib/ledger';
  import { ledgerEntryHref, ledgerHref, projectHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import LedgerIncoming from './LedgerIncoming.svelte';
  import LedgerLinks from './LedgerLinks.svelte';

  let {
    projectId,
    entryName,
    revision,
  }: { projectId: string; entryName: string; revision: number | null } = $props();
  const loading = $derived(api.ledgerEntry(projectId, entryName, revision));
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
  <a href={ledgerHref(projectId)}>Ledger</a> /
</nav>

{#await loading}
  <h1>{entryName}</h1>
  <p class="muted">Loading…</p>
{:then answer}
  {@const entry = answer.entry}
  <h1>{entry.handle}: {entry.title}</h1>
  <dl class="facts">
    <dt>Type</dt>
    <dd>
      {TYPE_NAME[entry.type]}{#if entry.origin}, {entry.origin === 'published'
          ? 'published'
          : 'from this project'}{/if}
    </dd>
    <dt>Status</dt>
    <dd>{statusText(entry.status)}</dd>
    <dt>Revision</dt>
    <dd>
      {entry.revision} of {entry.head}
      {#if entry.revision > 1}<a href={ledgerEntryHref(projectId, entry.handle, entry.revision - 1)}
          >earlier</a
        >{/if}
      {#if entry.revision < entry.head}<a
          href={ledgerEntryHref(projectId, entry.handle, entry.revision + 1)}>later</a
        >
        <a href={ledgerEntryHref(projectId, entry.handle)}>current</a>{/if}
    </dd>
    <dt>Written</dt>
    <dd>{formatTime(entry.updated)} by {authorText(entry.author)}</dd>
    <dt>Reason</dt>
    <dd>{entry.reason}</dd>
  </dl>

  <h2>Statement</h2>
  <Markdown source={entry.statement} />

  {#if fieldRows(entry.fields).length}
    <h2>Fields</h2>
    <dl class="facts">
      {#each fieldRows(entry.fields) as row (row.name)}
        <dt>{row.name}</dt>
        <dd>{row.value}</dd>
      {/each}
    </dl>
  {/if}

  <h2>Links</h2>
  {#if entry.links.length}
    <LedgerLinks {projectId} groups={linksByKind(entry.links)} direction="out" />
  {:else}
    <p class="muted">This revision holds no links.</p>
  {/if}
  {#if entry.revision === entry.head}
    <LedgerIncoming {projectId} {entry} />
  {:else}
    <p class="muted">The links other entries hold to it are shown at its current revision.</p>
  {/if}
{:catch error}
  <h1>{entryName}</h1>
  <ErrorMessage {error} />
{/await}
