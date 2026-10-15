<!-- SPDX-License-Identifier: MIT -->
<!-- One proposal that waits for the user (docs/specs/project-memory.md, PM-2, PM-4): what
     it would do, its text exactly as every agent would read it, with a change's
     differences from the entry now, the entry as it is now, who proposed it, when and
     why (threat model, A3: ApprovalText). Only props, so a test renders it on the
     server. -->
<script lang="ts">
  import type { MemoryProposal } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { proposalState, proposalTitle, proposerText } from '../lib/memory';
  import { visible } from '../lib/text';
  import ApprovalText from './ApprovalText.svelte';

  let {
    proposal,
    busy,
    ondecide,
  }: { proposal: MemoryProposal; busy: boolean; ondecide: (accept: boolean) => void } = $props();
  const standing = $derived(proposalState(proposal));
  const before = $derived(
    proposal.action === 'change' && proposal.current !== null ? proposal.current.text : null,
  );
</script>

<article class="proposal">
  <p><strong>{proposalTitle(proposal)}</strong> <code>{proposal.proposal_id}</code></p>
  <p class="muted">
    Proposed {formatTime(proposal.proposed_at)} by
    <bdi>{visible(proposerText(proposal.proposer))}</bdi>, because:
    <bdi>{visible(proposal.reason)}</bdi>
  </p>
  {#if proposal.text !== null}
    <ApprovalText
      text={proposal.text}
      {before}
      preview
      label={proposal.action === 'add'
        ? 'The entry proposed, exactly as every agent would read it'
        : 'The text proposed, exactly as every agent would read it'}
    />
  {/if}
  {#if proposal.action !== 'add'}
    {#if proposal.current}
      <ApprovalText
        text={proposal.current.text}
        label={`The entry now, revision ${proposal.current.revision}`}
      />
    {:else}
      <p class="muted">The entry was removed.</p>
    {/if}
  {/if}
  {#if standing.note}<p role="note">{standing.note}</p>{/if}
  <button type="button" disabled={busy || !standing.acceptable} onclick={() => ondecide(true)}
    >Accept</button
  >
  <button type="button" disabled={busy} onclick={() => ondecide(false)}>Decline</button>
</article>

<style>
  .proposal {
    border: 1px solid var(--border, #ccc);
    padding: 0.5em 1em;
    margin-bottom: 1em;
    overflow-wrap: anywhere;
  }
</style>
