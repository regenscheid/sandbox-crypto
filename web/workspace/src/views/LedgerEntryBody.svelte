<!-- SPDX-License-Identifier: MIT -->
<!-- One ledger entry at a revision, as read (docs/specs/ledger.md, LG-1, LG-5, LG-8,
     LG-25): its statement, with its math, its own fields, its evidence, the review
     findings anchored to it, the links it holds and those other entries hold to it, each
     leading where it points. All of it is shown as text, the statement
     through the Markdown component. -->
<script lang="ts">
  import type { EntryView, SourceRead } from '../lib/api-types';
  import Markdown from '../lib/Markdown.svelte';
  import HiddenBanner from './HiddenBanner.svelte';
  import { formatTime } from '../lib/format';
  import { authorText, fieldRows, linksByKind, statusText, TYPE_NAME } from '../lib/ledger';
  import { ledgerEntryHref } from '../lib/router';
  import LedgerCheck from './LedgerCheck.svelte';
  import LedgerEvidence from './LedgerEvidence.svelte';
  import LedgerFindings from './LedgerFindings.svelte';
  import LedgerFlags from './LedgerFlags.svelte';
  import LedgerIncoming from './LedgerIncoming.svelte';
  import LedgerLinks from './LedgerLinks.svelte';
  import ResearchState from './ResearchState.svelte';
  import { STATE_FIELDS } from '../lib/researchState';

  let {
    projectId,
    entry,
    read = [],
    changed = async () => {},
  }: {
    projectId: string;
    entry: EntryView;
    /** For a reference: how far the project's sessions read its sources (CM-12). */
    read?: SourceRead[];
    /** Read the entry again, after the user's change to it. */
    changed?: () => Promise<void>;
  } = $props();
  const fields = $derived(fieldRows(entry.fields, projectId, entry.links, STATE_FIELDS));
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

<LedgerFlags {projectId} {entry} {changed} />
<ResearchState {projectId} {entry} {read} />

<h2>Statement</h2>
<Markdown
  source={entry.statement}
  pins={entry.pins ?? []}
  checks={entry.figure_checks ?? []}
  {projectId}
/>
<HiddenBanner text={entry.statement} links />

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

<LedgerEvidence {projectId} {entry} />
<LedgerCheck {projectId} {entry} {changed} />
<LedgerFindings {projectId} findings={entry.findings} />

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
