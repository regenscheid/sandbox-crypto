<!-- SPDX-License-Identifier: MIT -->
<!-- One proposal that waits for the user (docs/specs/project-memory.md, PM-2, PM-4): what
     it would do, beside the entry as it is now, who proposed it, when and why. Only
     props, so a test renders it on the server; texts through the Markdown component. -->
<script lang="ts">
  import type { MemoryProposal } from '../lib/api-types';
  import Markdown from '../lib/Markdown.svelte';
  import { formatTime } from '../lib/format';
  import { proposalState, proposalTitle, proposerText } from '../lib/memory';

  let {
    proposal,
    busy,
    ondecide,
  }: { proposal: MemoryProposal; busy: boolean; ondecide: (accept: boolean) => void } = $props();
  const standing = $derived(proposalState(proposal));
</script>

<article class="proposal">
  <p><strong>{proposalTitle(proposal)}</strong> <code>{proposal.proposal_id}</code></p>
  <p class="muted">
    Proposed {formatTime(proposal.proposed_at)} by <bdi>{proposerText(proposal.proposer)}</bdi>,
    because: <bdi>{proposal.reason}</bdi>
  </p>
  <div class="texts">
    {#if proposal.action !== 'add'}
      <section>
        <h4>
          Now{#if proposal.current}, revision {proposal.current.revision}{/if}
        </h4>
        {#if proposal.current}
          <Markdown source={proposal.current.text} />
        {:else}
          <p class="muted">Removed.</p>
        {/if}
      </section>
    {/if}
    {#if proposal.text !== null}
      <section>
        <h4>{proposal.action === 'add' ? 'The entry proposed' : 'The text proposed'}</h4>
        <Markdown source={proposal.text} />
      </section>
    {/if}
  </div>
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
  .texts {
    display: flex;
    gap: 1em;
  }
  .texts section {
    flex: 1;
    min-width: 0;
  }
</style>
