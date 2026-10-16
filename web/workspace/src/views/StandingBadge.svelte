<!-- SPDX-License-Identifier: MIT -->
<!-- How the ledger labels an entry or a work record (CM-11), as a list shows it: the label,
     its text, scope and who settled it in its title, and the entry's kind and session scope
     as tags. Only props, so a test renders it on the server. -->
<script lang="ts">
  import type { Standing } from '../lib/api-types';
  import { standingLine, standingView } from '../lib/researchState';

  let {
    projectId,
    standing,
    tags = [],
  }: { projectId: string; standing: Standing | null; tags?: string[] } = $props();
  const view = $derived(standingView(projectId, standing));
</script>

{#if view}<span class="standing {view.label}" title={standingLine(view)}>{view.word}</span
  >{/if}{#each tags as tag (tag)}
  <span class="tag">{tag}</span>{/each}

<style>
  .standing,
  .tag {
    border: 1px solid var(--border, #9996);
    border-radius: 0.6em;
    font-size: 0.8em;
    padding: 0 0.4em;
    margin-left: 0.3em;
  }
  .standing.impossible {
    border-color: var(--failed, #c00);
  }
  .standing.claimed,
  .standing.disputed,
  .standing.flagged {
    border-color: var(--approximate, #b70);
  }
</style>
