<!-- SPDX-License-Identifier: MIT -->
<!-- The project memory (docs/specs/project-memory.md, PM-4, PM-7): the proposals that
     wait for the user, then the accepted entries in the order agents receive them, with
     the flag for a memory longer than they receive, and a form for a new entry. Every
     change names the revision shown; after each, and after a conflict, the page reads
     the memory again. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { browserState } from '../lib/browserState';
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
  import { visible } from '../lib/text';
  import { idle } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  import PaneResizer from './PaneResizer.svelte';
  import MemoryEntryItem from './MemoryEntryItem.svelte';
  import MemoryProposalCard from './MemoryProposalCard.svelte';

  let { projectId }: { projectId: string } = $props();
  let shown = $state<MemoryAnswer | null>(null);
  let proposals = $state<MemoryProposal[]>([]);
  let failure = $state<unknown>(null);
  const saving = $state(idle());
  const scope = untrack(() => `memory:${projectId}`);
  const kept = untrack(() => browserState.load(scope).draft ?? {});
  let adding = $state(kept.adding ?? '');
  let search = $state(kept.search ?? '');
  let selected = $state(kept.selected ?? '');
  let section = $state(kept.section ?? 'accepted');
  let viewportWidth = $state(window.innerWidth);
  const maxWidth = $derived(Math.max(320, Math.floor(viewportWidth / 2)));
  let width = $state(
    Math.min(Number(kept.width) || 460, Math.max(320, Math.floor(window.innerWidth / 2))),
  );
  $effect(() => {
    browserState.save(scope, { adding, search, selected, section, width: String(width) });
  });
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

<svelte:window
  onresize={() => {
    viewportWidth = window.innerWidth;
    width = Math.min(width, maxWidth);
  }}
/>

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
  <p class="muted">
    Agent budget: {shown.bound.toLocaleString()} characters. Accepted entries retain their receive order
    when filtered. Version {shown.memory.version}; {shown.memory.characters.toLocaleString()} characters.
  </p>
  {#if overflow}<p role="alert">{overflow}</p>{/if}
  <div class="memory-tools">
    <button aria-pressed={section === 'accepted'} onclick={() => (section = 'accepted')}
      >Accepted ({shown.memory.entries.length})</button
    >
    <button aria-pressed={section === 'proposals'} onclick={() => (section = 'proposals')}
      >Proposal inbox ({proposals.length})</button
    >
    <label>Search memory <input type="search" bind:value={search} /></label>
    <button onclick={() => void load()}>Refresh memory</button>
  </div>
  <div class="memory-columns" style:--detail-width={`${width}px`}>
    <nav aria-label="Memory records">
      {#if section === 'accepted'}
        {#each shown.memory.entries as entry, index (entry.entry_id)}
          {#if `${entry.entry_id} ${entry.text}`
            .toLocaleLowerCase()
            .includes(search.toLocaleLowerCase())}
            <button
              class="memory-row"
              aria-pressed={selected === entry.entry_id}
              onclick={() => (selected = entry.entry_id)}
            >
              <strong>{index + 1}. {entry.entry_id}</strong><span
                >{visible(entry.text.slice(0, 160))}</span
              ><small
                >{index < shown.memory.given ? 'Given to agents' : 'Past agent budget'} · revision {entry.revision}</small
              >
            </button>
          {/if}
        {/each}
        {#if shown.memory.entries.length === 0}<p>No accepted entries.</p>{/if}
      {:else}
        {#each proposals as proposal (proposal.proposal_id)}
          {#if `${proposal.proposal_id} ${proposal.text ?? ''} ${proposal.reason}`
            .toLocaleLowerCase()
            .includes(search.toLocaleLowerCase())}
            <button
              class="memory-row"
              aria-pressed={selected === proposal.proposal_id}
              onclick={() => (selected = proposal.proposal_id)}
              ><strong>{proposal.action} · {proposal.proposal_id}</strong><span
                >{visible(proposal.text?.slice(0, 160) ?? proposal.reason)}</span
              ></button
            >
          {/if}
        {/each}
        {#if proposals.length === 0}<p>No proposals wait.</p>{/if}
      {/if}
    </nav>
    <PaneResizer bind:width {maxWidth} label="Resize memory details" />
    <section aria-label="Selected memory record">
      <div class="entries">
        {#each shown.memory.entries as entry, index (entry.entry_id)}
          <ol hidden={section !== 'accepted' || selected !== entry.entry_id}>
            <MemoryEntryItem
              {projectId}
              {entry}
              first={index === 0}
              last={index === shown.memory.entries.length - 1}
              given={index < shown.memory.given}
              busy={saving.busy}
              onchange={change}
            />
          </ol>
        {/each}
      </div>
      {#each proposals as proposal (proposal.proposal_id)}
        <div hidden={section !== 'proposals' || selected !== proposal.proposal_id}>
          <MemoryProposalCard
            {proposal}
            busy={saving.busy}
            ondecide={(accept) => void change(decision(proposal, accept))}
          />
        </div>
      {/each}
      {#if !selected}<p>
          Select an accepted entry or proposal to review its exact text, differences and history.
        </p>{/if}
    </section>
  </div>

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
  .memory-tools {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: end;
    margin: 1rem 0;
  }
  .memory-columns {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 8px minmax(320px, var(--detail-width));
    border: 1px solid var(--border);
  }
  .memory-columns > nav,
  .memory-columns > section {
    padding: 0.75rem;
    min-width: 0;
    max-height: 70vh;
    overflow: auto;
  }
  .memory-row {
    display: grid;
    gap: 0.35rem;
    width: 100%;
    text-align: left;
    padding: 0.75rem;
    overflow-wrap: anywhere;
    border: 0;
    border-bottom: 1px solid var(--border);
  }
  .memory-row[aria-pressed='true'] {
    background: var(--surface-raised);
  }
  .entries {
    padding: 0;
    list-style: none;
  }
  @media (max-width: 800px) {
    .memory-columns {
      display: block;
    }
    .memory-columns :global(.pane-resizer) {
      display: none;
    }
  }

  textarea {
    width: 100%;
    box-sizing: border-box;
  }
</style>
