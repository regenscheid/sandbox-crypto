<!-- SPDX-License-Identifier: MIT -->
<!-- One proposal that waits for the user (docs/specs/project-memory.md, PM-2, PM-4): what
     it would do, its text exactly as every agent would read it, with a change's
     differences from the entry now, the entry as it is now, who proposed it, when and
     why (threat model, A3: ApprovalText). Only props, so a test renders it on the
     server. -->
<script lang="ts">
  import type { MemoryProposal } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { anchorText, basisText, proposalState, proposalTitle, proposerText } from '../lib/memory';
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
  <p class="muted">
    Basis: <strong>{basisText(proposal.basis)}</strong>
    {#if proposal.anchors.length > 0}
      · anchors:
      {#each proposal.anchors as anchor, index (index)}<code>{visible(anchorText(anchor))}</code
        >{' '}{/each}
    {:else}
      · no anchors
    {/if}
    . Accepting it does not make the text the user's.
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
  <button
    type="button"
    class="primary-button"
    disabled={busy || !standing.acceptable}
    onclick={() => ondecide(true)}>Accept</button
  >
  <button type="button" disabled={busy} onclick={() => ondecide(false)}>Decline</button>
</article>

<style>
  .proposal {
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 12px;
    overflow-wrap: anywhere;
  }
  .proposal > button {
    margin: 8px 6px 0 0;
  }
</style>
