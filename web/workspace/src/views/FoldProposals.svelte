<!-- SPDX-License-Identifier: MIT -->
<!-- A session's fold proposals waiting (docs/specs/context-management.md, CM-15): the
     research-state updates a fold's model wrote from folded material, each with what it
     proposes, its basis, its words as text, its anchors leading to the archived messages,
     and the fold that proposed it. Accept records it in the ledger as the user's change,
     keeping the model's basis, marked accepted by the user; Dismiss takes a reason. After
     either, and after a failure, the list is read again. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { ProposalView } from '../lib/api-types';
  import { proposalLine } from '../lib/folds';
  import { guardedWrite } from '../lib/mutation';
  import { idle, submit } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  import FoldProposalCard from './FoldProposalCard.svelte';

  let { projectId, sessionId }: { projectId: string; sessionId: string } = $props();
  let waiting = $state<ProposalView[] | null>(null);
  let failure = $state<unknown>(null);
  const saving = $state(idle());

  async function load() {
    try {
      waiting = await api.foldProposals(projectId, sessionId);
      failure = null;
    } catch (caught) {
      failure = caught;
    }
  }
  void load();

  function accept(proposal: string): Promise<boolean> {
    return submit(
      saving,
      async () => {
        await guardedWrite(
          'fold-proposal-accept',
          `${projectId}:${sessionId}:${proposal}`,
          {},
          (body) => api.acceptFoldProposal(projectId, sessionId, proposal, body),
        );
        await load();
      },
      load,
    );
  }
  function dismiss(proposal: string, reason: string): Promise<boolean> {
    return submit(
      saving,
      async () => {
        await api.dismissFoldProposal(projectId, sessionId, proposal, reason);
        await load();
      },
      load,
    );
  }
  const lines = $derived((waiting ?? []).map((found) => proposalLine(projectId, found)));
</script>

{#if failure}
  <ErrorMessage error={failure} />
{:else if lines.length}
  <section class="fold-proposals">
    <h2>Fold proposals waiting ({lines.length})</h2>
    <p class="muted">
      Written by a model from folded messages, not by you or the main agent: read each against its
      anchors. Accepting records it in the ledger as your change, marked accepted by the user;
      nothing changes until you decide.
    </p>
    {#if saving.error}<ErrorMessage error={saving.error} />{/if}
    {#each lines as line (line.id)}
      <FoldProposalCard {line} busy={saving.busy} {accept} {dismiss} />
    {/each}
  </section>
{/if}
