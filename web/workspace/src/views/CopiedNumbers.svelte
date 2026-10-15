<!-- SPDX-License-Identifier: MIT -->
<!-- The two numbers of a copied-number finding, lined up digit by digit (docs/specs/
     consistency-review.md, CR-39): the number stated above the one the artifact holds,
     40 places a row, each row with the place it starts at, and every place where they
     differ marked. Numbers too long to line up are shown whole, one above the other. Only
     props, so a test renders it on the server. -->
<script lang="ts">
  import { digitRows, lineUp } from '../lib/copiedNumbers';

  let { stated, held }: { stated: string; held: string } = $props();
  const columns = $derived(lineUp(stated, held));
  const rows = $derived(columns === null ? null : digitRows(columns));
  const differ = $derived(columns?.filter((c) => c.stated !== c.held).length ?? 0);
  const summary = $derived(`${differ} ${differ === 1 ? 'place differs' : 'places differ'}`);
  const places = $derived(String(columns?.length ?? 0).length);
  const at = (from: number) => `${String(from).padStart(places)} `;
</script>

<div class="copied">
  {#if rows}
    <p>
      The two numbers the statement names, digit by digit: {summary}, marked; – stands for a digit
      the other number has.
    </p>
    {#each rows as row (row.from)}
      <pre class="digits"><span class="muted">Stated          {at(row.from)}</span
        >{#each row.columns as c, k (k)}{#if c.stated === c.held}{c.stated}{:else}<mark
              >{c.stated || '–'}</mark
            >{/if}{/each}{'\n'}<span class="muted">In the artifact {at(row.from)}</span
        >{#each row.columns as c, k (k)}{#if c.stated === c.held}{c.held}{:else}<mark
              >{c.held || '–'}</mark
            >{/if}{/each}</pre>
    {/each}
  {:else}
    <p>The two numbers the statement names, too long to line up here:</p>
    <pre class="digits">Stated:          {stated}{'\n'}In the artifact: {held}</pre>
  {/if}
</div>

<style>
  .digits {
    white-space: pre;
    overflow-x: auto;
    max-height: none;
    margin: 0.2em 0;
  }
  .digits mark {
    font-weight: bold;
  }
</style>
