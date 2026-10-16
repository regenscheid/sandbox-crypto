<!-- SPDX-License-Identifier: MIT -->
<!-- A run's skills (SK-24): each discovery notice, with what it answered, how it matched
     (by words, or by meaning too), the skills it showed and whether a model call saw it;
     and the skills the run loaded, read or searched for, with how each went. Nothing shows
     for a run that had no skills. -->
<script lang="ts">
  import type { RunRecord } from '../lib/api';
  import { runSkills } from '../lib/skills';

  let { run }: { run: Pick<RunRecord, 'tools' | 'summaries' | 'delivered'> } = $props();
  const found = $derived(runSkills(run));
</script>

{#if found.discoveries.length || found.uses.length}
  <details class="run-skills">
    <summary>
      Skills: {found.discoveries.length}
      {found.discoveries.length === 1 ? 'discovery' : 'discoveries'}, {found.uses.length}
      {found.uses.length === 1 ? 'use' : 'uses'}
    </summary>
    {#if found.discoveries.length}
      <h4>Shown by discovery</h4>
      <ul>
        {#each found.discoveries as discovery, index (index)}
          <li>
            For the {discovery.answered}, compared {discovery.scoring}:
            {#if discovery.skills.length}
              {discovery.skills.join(', ')}{#if discovery.entry.length}; entry skills {discovery.entry.join(
                  ', ',
                )}{/if}.
              {#if discovery.seen === true}
                A model call saw the notice.
              {:else if discovery.seen === false}
                <span class="muted">No model call saw the notice.</span>
              {/if}
            {:else}
              <span class="muted">nothing shown.</span>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if found.uses.length}
      <h4>Loaded, read and searched</h4>
      <ul>
        {#each found.uses as use, index (index)}
          <li><code>{use.skill}</code> {use.how} <span class="muted">({use.detail})</span></li>
        {/each}
      </ul>
    {/if}
  </details>
{/if}
