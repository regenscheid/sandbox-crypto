<!-- SPDX-License-Identifier: MIT -->
<!-- One fold proposal (CM-15): what it proposes, its basis, its words as text, its anchors
     and the fold that proposed it, with Accept, and Dismiss with a reason. Only props and
     callbacks, so a test renders it on the server. -->
<script lang="ts">
  import type { ProposalLine } from '../lib/folds';

  let {
    line,
    busy = false,
    accept,
    dismiss,
  }: {
    line: ProposalLine;
    busy?: boolean;
    accept: (proposal: string) => Promise<boolean>;
    dismiss: (proposal: string, reason: string) => Promise<boolean>;
  } = $props();
  let reason = $state('');
  let dismissing = $state(false);
</script>

{#snippet link(target: { text: string; href: string | null })}
  {#if target.href}<a href={target.href}>{target.text}</a>{:else}<bdi>{target.text}</bdi>{/if}
{/snippet}

<article class="proposal">
  <h3>{line.kind} <span class="muted">· <code>{line.id}</code></span></h3>
  <dl class="facts">
    <dt>Written by</dt>
    <dd>a fold's model, from folded messages: fold {@render link(line.fold)}</dd>
    <dt>Basis</dt>
    <dd>{line.basis}</dd>
    {#each line.fields as [name, value] (name)}
      <dt>{name}</dt>
      <dd class="text">{value}</dd>
    {/each}
    {#if line.anchors.length}
      <dt>Anchors</dt>
      <dd>
        <ul>
          {#each line.anchors as anchor, i (i)}<li>{@render link(anchor)}</li>{/each}
        </ul>
      </dd>
    {/if}
  </dl>
  {#if dismissing}
    <form
      onsubmit={async (event) => {
        event.preventDefault();
        if (reason.trim() && (await dismiss(line.id, reason))) dismissing = false;
      }}
    >
      <label>
        Why you dismiss it
        <textarea rows="2" maxlength="4096" bind:value={reason}></textarea>
      </label>
      <button type="submit" disabled={busy || !reason.trim()}>Dismiss</button>
      <button type="button" onclick={() => (dismissing = false)}>Cancel</button>
    </form>
  {:else}
    <button type="button" disabled={busy} onclick={() => void accept(line.id)}>Accept</button>
    <button type="button" disabled={busy} onclick={() => (dismissing = true)}>Dismiss…</button>
  {/if}
</article>

<style>
  .proposal {
    border-top: 1px solid var(--border, #ccc);
    padding-top: 0.5rem;
  }
  .text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  textarea {
    display: block;
    width: 100%;
  }
</style>
