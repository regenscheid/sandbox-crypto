<!-- SPDX-License-Identifier: MIT -->
<!-- An entry's links of each kind, read from the entry ("Assumes A-1") or toward it
     ("Assumed by C-2"), each leading to its target where the workspace shows one. -->
<script lang="ts">
  import type { EntryLink } from '../lib/api-types';
  import { linkTarget, LINK_WORDS, type LinkKind } from '../lib/ledger';

  let {
    projectId,
    groups,
    direction,
  }: { projectId: string; groups: [LinkKind, EntryLink[]][]; direction: 'out' | 'in' } = $props();
</script>

<dl class="facts">
  {#each groups as [kind, links] (kind)}
    <dt>{LINK_WORDS[kind][direction]}</dt>
    <dd>
      <ul class="links">
        {#each links as link, index (index)}
          {@const target = linkTarget(projectId, link, direction)}
          <li>
            {#if target.href}<a href={target.href}>{target.text}</a>{:else}{target.text}{/if}
            {#if link.note}<span class="muted">— {link.note}</span>{/if}
          </li>
        {/each}
      </ul>
    </dd>
  {/each}
</dl>
