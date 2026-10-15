<!-- SPDX-License-Identifier: MIT -->
<!-- One `dominates` link as the frontier shows it (docs/specs/ledger.md, LG-22): the
     result that holds it and the one it names, each leading to its entry, its reason,
     who recorded it in which revision of its holder, why it counts for nothing when it
     does, and the user's "Drop this link…", which opens the drop panel in place. -->
<script lang="ts">
  import type { DominatesLink } from '../lib/api-types';
  import { recordedText } from '../lib/frontierLinks';
  import { ENTRY_NAME, ledgerEntryHref } from '../lib/router';
  import FrontierDrop from './FrontierDrop.svelte';

  let {
    projectId,
    link,
    done,
  }: {
    projectId: string;
    link: DominatesLink;
    /** After the user's drop: what to say, and the frontier read again. */
    done: (text: string) => Promise<void>;
  } = $props();
  let dropping = $state(false);
</script>

{#snippet entry(handle: string)}{#if ENTRY_NAME.test(handle)}<a
      href={ledgerEntryHref(projectId, handle)}>{handle}</a
    >{:else}{handle}{/if}{/snippet}

<li>
  {@render entry(link.handle)} dominates {@render entry(link.target)} by a link: {link.reason ||
    'it gives no reason'}
  <span class="muted">({recordedText(link)})</span>
  {#if link.void}<br /><strong class="flagged">It counts for nothing: {link.void}.</strong>{/if}
  {#if dropping}
    <FrontierDrop
      {projectId}
      {link}
      done={async (text) => {
        dropping = false;
        await done(text);
      }}
      oncancel={() => (dropping = false)}
    />
  {:else}
    <button type="button" onclick={() => (dropping = true)}>Drop this link…</button>
  {/if}
</li>
