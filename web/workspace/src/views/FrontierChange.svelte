<!-- SPDX-License-Identifier: MIT -->
<!-- One change to a category's best known results (docs/specs/ledger.md, LG-24): when,
     under which conditions, what it added and removed, the best known it left, each
     leading to its entry, and its cause: a ledger change with its reason, the results it
     changed and the dominates links it added or dropped, the category's revision, or the
     history's beginning. All of it text. Only props, so a test renders it on the server. -->
<script lang="ts">
  import type { FrontierChange } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { conditionsText } from '../lib/frontier';
  import { causeText, changeMore } from '../lib/frontierHistory';
  import { ENTRY_NAME, ledgerEntryHref } from '../lib/router';

  let { projectId, change }: { projectId: string; change: FrontierChange } = $props();
  const cause = $derived(causeText(change.cause));
</script>

{#snippet entry(handle: string, revision?: number)}{#if ENTRY_NAME.test(handle)}<a
      href={ledgerEntryHref(projectId, handle, revision)}
      >{handle}{revision === undefined ? '' : ` r${revision}`}</a
    >{:else}{handle}{revision === undefined ? '' : ` r${revision}`}{/if}{/snippet}

<li>
  <p>
    <strong>{formatTime(change.created)}</strong>, {conditionsText(change.conditions)}
  </p>
  {#if change.added.length}
    <p>
      Added: {#each change.added as handle, index (index)}{index > 0 ? ', ' : ''}{@render entry(
          handle,
        )}{/each}{changeMore(change, 'added')}
    </p>
  {/if}
  {#if change.removed.length}
    <p>
      Removed: {#each change.removed as handle, index (index)}{index > 0 ? ', ' : ''}{@render entry(
          handle,
        )}{/each}{changeMore(change, 'removed')}
    </p>
  {/if}
  <p>
    Best known after it: {#each change.best as member, index (index)}{index > 0
        ? ', '
        : ''}{@render entry(member.handle, member.revision)}{:else}none{/each}{changeMore(
      change,
      'best',
    )}
  </p>
  <p class="muted">
    {cause.summary}{#if cause.entries.length}, to {#each cause.entries as handle, index (index)}{index >
        0
          ? ', '
          : ''}{@render entry(handle)}{/each}{/if}.
  </p>
  {#if cause.reason !== null}<p class="muted">Its reason: {cause.reason}</p>{/if}
  {#if cause.links.length}
    <ul class="links">
      {#each cause.links as link, index (index)}<li>{link}</li>{/each}
    </ul>
  {/if}
</li>
