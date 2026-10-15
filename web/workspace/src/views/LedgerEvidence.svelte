<!-- SPDX-License-Identifier: MIT -->
<!-- An entry's evidence (docs/specs/ledger.md, LG-8 to LG-11): each link's role and type,
     its pinned target, leading there where the workspace shows it, its scope note and the
     revision that added it; "asserted only" for a claim or result without any. The role,
     type and scope are the author's assertion, shown as text. -->
<script lang="ts">
  import type { EntryView } from '../lib/api-types';
  import {
    EVIDENCE_SHOWN,
    evidenceKind,
    evidenceState,
    evidenceTarget,
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
        <li>
          <strong>{evidenceKind(link)}</strong>:
          {#if target.href}<a href={target.href}>{target.text}</a>{:else}{target.text}{/if}
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
