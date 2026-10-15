<!-- SPDX-License-Identifier: MIT -->
<!-- One work record (docs/specs/ledger.md, LG-6, LG-12): how it ended and its detail, the
     entry's revision then, who wrote it and when, what was tried, the entries it produced
     and its pinned references, each leading where the workspace shows it, with its note,
     the record it is part of, and its reason. The author's text is shown as text, each
     character a person may not see as its code point. Only props, so a test renders it on
     the server; `children` holds what the entry view offers on it (an assessment). -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { WorkRecord } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { authorText, provenanceText } from '../lib/ledger';
  import {
    checkView,
    detailText,
    endingText,
    producedLinks,
    referenceView,
  } from '../lib/ledgerWork';
  import { CATEGORY_NAME, treeHref } from '../lib/router';
  import { visible } from '../lib/text';

  let {
    projectId,
    record,
    read = [],
    children,
  }: {
    projectId: string;
    record: WorkRecord;
    /** The records read with it, to name a record it assesses or cites. */
    read?: readonly WorkRecord[];
    children?: Snippet;
  } = $props();
  const detail = $derived(detailText(record, read));
  const check = $derived(checkView(record));
  // The run tree of the session the check ran in, when the session is one the server names.
  const tree = $derived(
    check?.session && CATEGORY_NAME.test(check.session) ? treeHref(projectId, check.session) : null,
  );
  const produced = $derived(producedLinks(projectId, record));
  const references = $derived(record.references.map((r) => referenceView(projectId, r, read)));
</script>

<li class="work">
  <p>
    <strong class:claimed={record.ending === 'obstruction_claimed'}>{endingText(record)}</strong>
    <span class="muted"
      >on r{record.entry_revision}, {formatTime(record.created)}, by {authorText(record.author)}
      · <code>{record.record_id}</code></span
    >
  </p>
  {#if detail}
    <p>
      {detail}{#if tree}
        <a href={tree}>The run tree</a> shows its brief and outcome.{/if}
    </p>
  {/if}
  <p class="label">What was tried</p>
  <p class="tried">{visible(record.tried)}</p>
  {#if produced.length}
    <p>
      Produced: {#each produced as entry, index (index)}{index > 0 ? ', ' : ''}{#if entry.href}<a
            href={entry.href}>{entry.text}</a
          >{:else}{entry.text}{/if}{/each}
    </p>
  {/if}
  {#if references.length}
    <p class="label">{record.ending === 'obstruction_claimed' ? 'Its evidence' : 'It cites'}</p>
    <ul class="links">
      {#each references as reference, index (index)}
        <li>
          {#if reference.href}<a href={reference.href}>{reference.text}</a
            >{:else}{reference.text}{/if}{#if reference.note}: <span class="tried"
              >{reference.note}</span
            >{/if}
        </li>
      {/each}
    </ul>
  {/if}
  {#if record.part_of}<p class="muted">Part of work record {visible(record.part_of)}</p>{/if}
  <p class="muted">
    Reason: {visible(record.reason)}{#if record.provenance}; {provenanceText(
        record.provenance,
      )}{/if}
  </p>
  {#if children}{@render children()}{/if}
</li>

<style>
  .work {
    border-top: 1px solid var(--border, #9996);
    padding: 0.4em 0;
    list-style: none;
  }
  .work p {
    margin: 0.2em 0;
  }
  .tried {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .label {
    font-size: 0.9em;
  }
  .claimed {
    color: var(--approximate);
  }
</style>
