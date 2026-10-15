<!-- SPDX-License-Identifier: MIT -->
<!-- The gate for an entry and the decisions that affect it, as read (docs/specs/ledger.md,
     LG-4, LG-17): each clause, whether it holds and why; the decisions in effect, then the
     others, each leading to the decision, whose own page has its history. All of it text.
     Only props, so a test renders it on the server. -->
<script lang="ts">
  import type { GateView } from '../lib/api-types';
  import { authorText, statusText } from '../lib/ledger';
  import { CLAUSE_WORDS, DECISIONS_LISTED, decisionGroups, gateSummary } from '../lib/ledgerGate';
  import { ENTRY_NAME, ledgerEntryHref } from '../lib/router';

  let { projectId, gate, shown }: { projectId: string; gate: GateView; shown: number } = $props();
  const groups = $derived(decisionGroups(gate.decisions));
</script>

<h2>Gate and decisions</h2>
{#if shown !== gate.revision}
  <p class="muted">Read on the entry as it is now, r{gate.revision}, not the r{shown} shown.</p>
{/if}
<p>{gateSummary(gate)}</p>
{#if gate.applies}
  <ul class="gate">
    {#each gate.clauses as clause (clause.clause)}
      <li>
        <strong class:holds={clause.holds} class:fails={!clause.holds}
          >{clause.holds ? 'Holds' : 'Not yet'}</strong
        >: {CLAUSE_WORDS[clause.clause]}. <span class="muted">{clause.reason}</span>
      </li>
    {/each}
  </ul>
{/if}
{#snippet decision(item: GateView['decisions'][number])}
  <li>
    {#if ENTRY_NAME.test(item.handle)}<a href={ledgerEntryHref(projectId, item.handle)}
        >{item.handle}</a
      >{:else}{item.handle}{/if}
    r{item.revision}, {statusText(item.status)}: {item.title}
    <span class="muted">({authorText(item.author)})</span>
  </li>
{/snippet}
{#if gate.decisions.length}
  {#if groups.inEffect.length}
    <h3>Decisions in effect</h3>
    <ul class="decisions">
      {#each groups.inEffect as item (item.entry_id)}{@render decision(item)}{/each}
    </ul>
  {/if}
  {#if groups.others.length}
    <h3>Other decisions that affect it</h3>
    <ul class="decisions">
      {#each groups.others as item (item.entry_id)}{@render decision(item)}{/each}
    </ul>
  {/if}
  {#if gate.decisions.length >= DECISIONS_LISTED}
    <p class="muted">The newest {gate.decisions.length} decisions are listed.</p>
  {/if}
{:else}
  <p class="muted">No decision affects it.</p>
{/if}
