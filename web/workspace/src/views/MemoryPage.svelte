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
  import { visible } from '../lib/text';
  import { idle } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  import MemoryEntryItem from './MemoryEntryItem.svelte';
  import MemoryProposalCard from './MemoryProposalCard.svelte';

  let { projectId }: { projectId: string } = $props();
  let shown = $state<MemoryAnswer | null>(null);
  let proposals = $state<MemoryProposal[]>([]);
  let failure = $state<unknown>(null);
  const saving = $state(idle());
  const scope = untrack(() => `memory:${projectId}`);
  const restored = untrack(() => browserState.load(scope));
  const kept = restored.draft ?? {};
  let stored = $state(restored.stored);
  let adding = $state(kept.adding ?? '');
  let search = $state(kept.search ?? '');
  let selected = $state(kept.selected ?? '');
  let section = $state(kept.section ?? 'accepted');
  $effect(() => {
    stored = browserState.save(scope, { adding, search, selected, section });
  });
  const matches = (text: string) => text.toLocaleLowerCase().includes(search.toLocaleLowerCase());
  /** The first line of an entry, for its row; the whole text shows when it is opened. */
  const firstLine = (text: string) => {
    const line = text.trim().split('\n')[0] ?? '';
    return line.length > 200 ? `${line.slice(0, 200)}…` : line;
  };
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

<div class="page-head">
  <div>
    <h1>Memory</h1>
    <p class="muted">
      Notes every agent in this project receives when it starts: conventions and notation, settled
      facts about the environment and the tools, your preferences, and pitfalls to avoid. Agents
      propose; nothing changes until you decide.
    </p>
  </div>
  <label class="sr-only" for="memory-search">Search memory</label>
  <input id="memory-search" type="search" placeholder="Search memory" bind:value={search} />
</div>
{#if restored.damaged}
  <p role="alert">
    Saved memory browser data could not be restored. Check any new entry before continuing.
  </p>
{/if}
{#if saving.error}<ErrorMessage error={saving.error} />{/if}
{#if failure}
  <ErrorMessage error={failure} />
{:else if shown === null}
  <p class="muted">Loading…</p>
{:else}
  {@const used = shown.memory.characters}
  <div class="budget">
    <p class="muted">
      {used.toLocaleString()} of {shown.bound.toLocaleString()} characters given to agents · version
      {shown.memory.version}
    </p>
    <div
      class="bar"
      role="meter"
      aria-label="Memory given to agents"
      aria-valuemin="0"
      aria-valuemax={shown.bound}
      aria-valuenow={Math.min(used, shown.bound)}
    >
      <span
        class:over={used > shown.bound}
        style:width={`${Math.min(100, (100 * used) / shown.bound)}%`}
      ></span>
    </div>
  </div>
  {#if overflow}<p role="alert">{overflow}</p>{/if}
  <div class="memory-tools">
    <div class="segmented" role="tablist" aria-label="Memory">
      <button
        type="button"
        role="tab"
        aria-selected={section === 'accepted'}
        onclick={() => (section = 'accepted')}>Entries {shown.memory.entries.length}</button
      ><button
        type="button"
        role="tab"
        aria-selected={section === 'proposals'}
        onclick={() => (section = 'proposals')}
        >Proposals <span class:waiting={proposals.length > 0}>{proposals.length}</span></button
      >
    </div>
    <button type="button" class="link-button" onclick={() => void load()}>Refresh</button>
  </div>

  {#if section === 'accepted'}
    <ol class="memory-list" aria-label="Accepted entries, in the order agents receive them">
      {#each shown.memory.entries as entry, index (entry.entry_id)}
        <li hidden={!matches(`${entry.entry_id} ${entry.text}`)}>
          <button
            type="button"
            class="memory-row"
            aria-expanded={selected === entry.entry_id}
            onclick={() => (selected = selected === entry.entry_id ? '' : entry.entry_id)}
          >
            <span class="entry-number">{index + 1}</span>
            <span class="entry-text">{visible(firstLine(entry.text))}</span>
            {#if index >= shown.memory.given}<span class="pill over">Past agent budget</span>{/if}
            <span class="chevron" aria-hidden="true">›</span>
          </button>
          {#if selected === entry.entry_id}
            <ol class="entry-detail">
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
          {/if}
        </li>
      {/each}
    </ol>
    {#if shown.memory.entries.length === 0}<p class="empty-note">
        No entries yet. Add the first one below, or accept one an agent proposes.
      </p>{/if}
  {:else}
    <div class="proposals">
      {#each proposals as proposal (proposal.proposal_id)}
        <div hidden={!matches(`${proposal.proposal_id} ${proposal.text ?? ''} ${proposal.reason}`)}>
          <MemoryProposalCard
            {proposal}
            busy={saving.busy}
            ondecide={(accept) => void change(decision(proposal, accept))}
          />
        </div>
      {/each}
      {#if proposals.length === 0}<p class="empty-note">No proposals wait for you.</p>{/if}
    </div>
  {/if}

  <section class="add" aria-label="Add an entry">
    <h2>Add an entry</h2>
    <textarea
      aria-label="A new entry"
      rows="3"
      placeholder="A convention, a settled fact about the tools, a preference, or a pitfall to avoid"
      bind:value={adding}></textarea>
    {#if adding !== '' && !stored}
      <p role="alert">
        This new memory entry is kept only in this tab. Copy it before reloading or closing the tab.
      </p>
    {/if}
    {#if problem}<p role="alert">{problem}</p>{/if}
    <button
      type="button"
      class="primary-button"
      disabled={saving.busy || adding.trim() === '' || problem !== null}
      onclick={add}>Add</button
    >
  </section>
{/if}

<style>
  .budget {
    max-width: 32rem;
    margin: 4px 0 16px;
  }
  .budget p {
    margin: 0 0 6px;
    font-size: 12px;
  }
  .bar {
    height: 6px;
    border-radius: 3px;
    background: var(--subtle);
    overflow: hidden;
  }
  .bar span {
    display: block;
    height: 100%;
    background: var(--accent);
  }
  .bar span.over {
    background: var(--warn-dot);
  }
  .memory-tools {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 8px;
  }
  .segmented {
    flex: 0 0 auto;
  }
  .segmented button {
    padding: 0 12px;
  }
  .segmented span.waiting {
    padding: 0 6px;
    border-radius: 8px;
    background: var(--warn-soft);
    color: var(--warn-text);
  }
  .memory-list {
    list-style: none;
    margin: 0;
    padding: 0;
    border-top: 1px solid var(--border);
  }
  .memory-list > li {
    border-bottom: 1px solid var(--border);
  }
  .memory-row {
    display: flex;
    align-items: baseline;
    gap: 12px;
    width: 100%;
    min-height: 0;
    padding: 12px 8px;
    border: 0;
    border-radius: 0;
    background: transparent;
    color: var(--text);
    font: inherit;
    text-align: left;
  }
  .memory-row:hover {
    background: var(--subtle);
  }
  .entry-number {
    flex: none;
    width: 1.5rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .entry-text {
    flex: 1 1 auto;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .chevron {
    flex: none;
    color: var(--muted);
    transition: transform 0.15s;
  }
  .memory-row[aria-expanded='true'] .chevron {
    transform: rotate(90deg);
  }
  .pill {
    flex: none;
    padding: 0 8px;
    border-radius: 9px;
    font-size: 12px;
  }
  .pill.over {
    background: var(--warn-soft);
    color: var(--warn-text);
  }
  .entry-detail {
    list-style: none;
    margin: 0;
    padding: 0 8px 14px 44px;
  }
  .add {
    margin-top: 28px;
    max-width: 48rem;
  }
  .add h2 {
    font-size: 14px;
    margin: 0 0 8px;
  }
  textarea {
    width: 100%;
    box-sizing: border-box;
    margin-bottom: 8px;
  }
</style>
