<!-- SPDX-License-Identifier: MIT -->
<!-- A run's folds (docs/specs/context-management.md, CM-21, CM-46), from its record: each
     fold or overview with its level and how it went, leading to its record's text in the
     session's archive; the markers its last rebuild retired, by record; and how often the
     folded region went over its budget. Nothing shows for a run that did not fold. -->
<script lang="ts">
  import type { RunRecord } from '../lib/api';
  import { runFolds } from '../lib/folds';

  let { run }: { run: Pick<RunRecord, 'summaries' | 'context'> } = $props();
  const found = $derived(runFolds(run.context.project_id, run));
</script>

{#if found && (found.folds.length || found.retired.length || found.overs)}
  <details class="run-folds">
    <summary>
      Folds: {found.folds.length}{#if found.overs}; the folded region went over its budget {found.overs}
        {found.overs === 1 ? 'time' : 'times'}{/if}
    </summary>
    {#if found.folds.length}
      <ul>
        {#each found.folds as fold, index (index)}
          <li>
            {fold.text}{#if fold.href}
              <a href={fold.href}>Its record</a>{/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if found.retired.length}
      <p>Markers retired at the run's last rebuild:</p>
      <ul>
        {#each found.retired as item, index (index)}
          <li>
            {item.count} in {#if item.href}<a href={item.href}>{item.text}</a>{:else}<bdi
                >{item.text}</bdi
              >{/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if found.weak}
      <p class="muted">
        {found.weak} weak {found.weak === 1 ? 'fold' : 'folds'}: each freed little.
      </p>
    {/if}
  </details>
{/if}
