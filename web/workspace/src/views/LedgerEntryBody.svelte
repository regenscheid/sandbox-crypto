<!-- SPDX-License-Identifier: MIT -->
<!-- One ledger entry at a revision, as read (docs/specs/ledger.md, LG-1, LG-5): its
     statement, with its math, its own fields, the links it holds and those other entries
     hold to it, each leading where it points. All of it is shown as text, the statement
     through the Markdown component. -->
<script lang="ts">
  import type { EntryView } from '../lib/api-types';
  import Markdown from '../lib/Markdown.svelte';
  import { formatTime } from '../lib/format';
  import { authorText, fieldRows, linksByKind, statusText, TYPE_NAME } from '../lib/ledger';
  import { ledgerEntryHref } from '../lib/router';
  import LedgerIncoming from './LedgerIncoming.svelte';
  import LedgerLinks from './LedgerLinks.svelte';

  let { projectId, entry }: { projectId: string; entry: EntryView } = $props();
  const fields = $derived(fieldRows(entry.fields, projectId));
</script>

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

{#if fields.length}
  <h2>Fields</h2>
  <dl class="facts">
    {#each fields as row (row.key)}
      <dt>{row.name}</dt>
      <dd>
        {#if row.href}<a href={row.href}>{row.value}</a>{:else}{row.value}{/if}
      </dd>
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
