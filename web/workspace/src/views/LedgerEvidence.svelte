<!-- SPDX-License-Identifier: MIT -->
<!-- An entry's evidence (docs/specs/ledger.md, LG-8 to LG-11): each link's role and type,
     its pinned target, leading there where the workspace shows it, its scope note and the
     revision that added it; "asserted only" for a claim or result without any. A quote
     names its reference, where in the PDF or ePrint it is, the PDF in the viewer at its
     page, and the quoted text exactly, each character a person may not see as its code
     point (CR-4). The role, type, scope and quote are the author's assertion, shown as
     text. -->
<script lang="ts">
  import type { EntryView } from '../lib/api-types';
  import {
    EVIDENCE_SHOWN,
    evidenceKind,
    evidenceState,
    evidenceTarget,
    quoteView,
  } from '../lib/ledgerEvidence';
  import { ledgerEntryHref } from '../lib/router';

  let { projectId, entry }: { projectId: string; entry: EntryView } = $props();
  let all = $state(false);
  const held = $derived(evidenceState(entry));
  const shown = $derived(all ? entry.evidence : entry.evidence.slice(0, EVIDENCE_SHOWN));
</script>

{#if held !== 'none'}
  <h2>Evidence</h2>
  {#if held === 'asserted'}
    <p><strong>Asserted only:</strong> this revision holds no evidence links.</p>
  {:else}
    <ul class="evidence">
      {#each shown as link (link.evidence_id)}
        {@const target = evidenceTarget(projectId, link)}
        {@const quote = quoteView(projectId, link)}
        <li>
          <strong>{evidenceKind(link)}</strong>:
          {#if quote}
            quote of {#if quote.reference.href}<a href={quote.reference.href}
                >{quote.reference.text}</a
              >{:else}{quote.reference.text}{/if}, {quote.where}{#if quote.pdf}:
              <a href={quote.pdf}>open the PDF at page {quote.page}</a>{/if}
            <pre class="quote" aria-label="The quoted text, exactly">{quote.text}</pre>
          {:else if target.href}<a href={target.href}>{target.text}</a>{:else}{target.text}{/if}
          {#if link.scope}<p class="scope">Scope: {link.scope}</p>{/if}
          <p class="muted">
            Added in <a href={ledgerEntryHref(projectId, entry.handle, link.added)}>r{link.added}</a
            >
          </p>
        </li>
      {/each}
    </ul>
    {#if entry.evidence.length > shown.length}
      <button type="button" onclick={() => (all = true)}
        >Show all {entry.evidence.length} evidence links</button
      >
    {/if}
  {/if}
{/if}

<style>
  .quote {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: none;
    margin: 0.3em 0;
    padding-left: 0.6em;
    border-left: 3px solid var(--border, #9996);
  }
</style>
