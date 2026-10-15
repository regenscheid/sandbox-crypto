<!-- SPDX-License-Identifier: MIT -->
<!-- An entry's evidence (docs/specs/ledger.md, LG-8 to LG-11): each link's role and type,
     its pinned target, leading there where the workspace shows it, its scope note and the
     revision that added it; "asserted only" for a claim or result without any. A quote
     names its reference, where in the PDF or ePrint it is, the PDF in the viewer at its
     page, and the quoted text exactly, each character a person may not see as its code
     point, with its citation checks, read when the revision holds a quote (CR-4). The role,
     type, scope and quote are the author's assertion, shown as text. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { EntryView, LedgerCitations } from '../lib/api-types';
  import { quoteChecks } from '../lib/citations';
  import { Detail, noDetail } from '../lib/detail';
  import {
    EVIDENCE_SHOWN,
    evidenceKind,
    evidenceState,
    evidenceTarget,
    quoteView,
  } from '../lib/ledgerEvidence';
  import { ledgerEntryHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import QuoteCitations from './QuoteCitations.svelte';

  let { projectId, entry }: { projectId: string; entry: EntryView } = $props();
  let all = $state(false);
  const held = $derived(evidenceState(entry));
  const shown = $derived(all ? entry.evidence : entry.evidence.slice(0, EVIDENCE_SHOWN));
  /** The citation checks of the revision's quotes, read when it holds any (CR-4). */
  const citations = $state(noDetail<string, LedgerCitations>());
  const checks = new Detail(citations, (key: string) => {
    const at = key.lastIndexOf('@');
    return api.ledgerCitations(projectId, key.slice(0, at), Number(key.slice(at + 1)));
  });
  $effect(() => {
    // Read again whenever the entry is: after the user's change, a check may have come.
    if (entry.evidence.some((link) => link.target === 'quote')) {
      void checks.show(`${entry.entry_id}@${entry.revision}`);
    }
  });
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
            <QuoteCitations
              {projectId}
              quote={citations.value === null
                ? null
                : (quoteChecks(citations.value.citations, link.evidence_id) ?? {
                    evidence_id: link.evidence_id,
                    checks: [],
                    total: 0,
                  })}
              loading={citations.loading}
            />
          {:else if target.href}<a href={target.href}>{target.text}</a>{:else}{target.text}{/if}
          {#if link.scope}<p class="scope">Scope: {link.scope}</p>{/if}
          <p class="muted">
            Added in <a href={ledgerEntryHref(projectId, entry.handle, link.added)}>r{link.added}</a
            >
          </p>
        </li>
      {/each}
    </ul>
    {#if citations.error}
      <p>The quotes' citation checks could not be read:</p>
      <ErrorMessage error={citations.error} />
    {/if}
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
