<!-- SPDX-License-Identifier: MIT -->
<!-- The project memory (docs/specs/project-memory.md, PM-4, PM-7): the proposals that
     wait for the user, then the accepted entries in the order agents receive them, with
     the flag for a memory longer than they receive, and a form for a new entry. Every
     change names the revision shown; after each, and after a conflict, the page reads
     the memory again. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { MemoryAnswer, MemoryProposal } from '../lib/api-types';
  import {
    changeMemory,
    decision,
    type MemoryAction,
    overflowText,
    textProblem,
  } from '../lib/memory';
  import { projectHref } from '../lib/router';
  import { idle } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  import MemoryEntryItem from './MemoryEntryItem.svelte';
  import MemoryProposalCard from './MemoryProposalCard.svelte';

  let { projectId }: { projectId: string } = $props();
  let shown = $state<MemoryAnswer | null>(null);
  let proposals = $state<MemoryProposal[]>([]);
  let failure = $state<unknown>(null);
  const saving = $state(idle());
  let adding = $state('');
  const problem = $derived(adding.trim() === '' ? null : textProblem(adding));
  const overflow = $derived(shown === null ? null : overflowText(shown.memory, shown.bound));

  async function load() {
    try {
      [shown, proposals] = await Promise.all([
        api.memory(projectId),
        api.memoryProposals(projectId),
      ]);
      failure = null;
    } catch (caught) {
      failure = caught;
    }
  }
  void load();

  function change(action: MemoryAction): Promise<boolean> {
    return changeMemory(
      saving,
      projectId,
      action,
      (body) => api.changeMemory(projectId, body),
      load,
    );
  }
  async function add() {
    if (problem === null && (await change({ action: 'add', text: adding }))) adding = '';
  }
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
</nav>
<h1>Project memory</h1>
<p>
  Notes on how to work in this project, which every agent of the project receives when it starts:
  conventions and notation, settled facts about the environment and the tools, your preferences, and
  pitfalls to avoid. Agents propose; nothing changes until you decide.
</p>
{#if saving.error}<ErrorMessage error={saving.error} />{/if}
{#if failure}
  <ErrorMessage error={failure} />
{:else if shown === null}
  <p class="muted">Loading…</p>
{:else}
  <h2>Proposals waiting ({proposals.length})</h2>
  {#if proposals.length === 0}
    <p class="muted">No proposals wait.</p>
  {:else}
    {#each proposals as proposal (proposal.proposal_id)}
      <MemoryProposalCard
        {proposal}
        busy={saving.busy}
        ondecide={(accept) => void change(decision(proposal, accept))}
      />
    {/each}
  {/if}

  <h2>Entries</h2>
  <p class="muted">
    Version {shown.memory.version}; {shown.memory.entries.length} entries, {shown.memory.characters.toLocaleString(
      'en',
    )} characters.
  </p>
  {#if overflow}<p role="alert">{overflow}</p>{/if}
  {#if shown.memory.entries.length === 0}
    <p class="muted">No entries yet.</p>
  {:else}
    <ol class="entries">
      {#each shown.memory.entries as entry, index (entry.entry_id)}
        <MemoryEntryItem
          {projectId}
          {entry}
          first={index === 0}
          last={index === shown.memory.entries.length - 1}
          given={index < shown.memory.given}
          busy={saving.busy}
          onchange={change}
        />
      {/each}
    </ol>
  {/if}

  <h3>Add an entry</h3>
  <textarea aria-label="A new entry" rows="4" bind:value={adding}></textarea>
  {#if problem}<p role="alert">{problem}</p>{/if}
  <button
    type="button"
    disabled={saving.busy || adding.trim() === '' || problem !== null}
    onclick={add}>Add</button
  >
{/if}

<style>
  textarea {
    width: 100%;
    box-sizing: border-box;
  }
</style>
