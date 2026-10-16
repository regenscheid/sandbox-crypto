<!-- SPDX-License-Identifier: MIT -->
<!-- An entry's research state (docs/specs/context-management.md, CM-10 to CM-12): how the
     ledger labels it, with the label's text, its scope, the assumptions it rests on and who
     settled it; its kind and, for a decision, the one session it is meant for; its reopen
     condition, saying when its flag is open; its caveats; and, for a reference, how far the
     project's sessions read each of its sources; and where the revision shown comes from
     (CM-9): its basis and its anchors, each leading where the workspace has a page, and
     beside a standing constraint, the user's words it quotes, which code found word for
     word in the user's message. Only props, so a test renders it on the server. -->
<script lang="ts">
  import type { EntryView, SourceRead } from '../lib/api-types';
  import { statusText } from '../lib/ledger';
  import { provenanceView, readText, standingView, stateFields } from '../lib/researchState';
  import { visible } from '../lib/text';

  let {
    projectId,
    entry,
    read = [],
  }: { projectId: string; entry: EntryView; read?: SourceRead[] } = $props();
  const standing = $derived(standingView(projectId, entry.standing));
  const state = $derived(stateFields(entry));
  const provenance = $derived(provenanceView(projectId, entry));
  const quotes = $derived(provenance.anchors.filter((anchor) => anchor.quote !== null));
  const shown = $derived(
    provenance.basis !== null ||
      provenance.anchors.length > 0 ||
      standing !== null ||
      entry.kind !== null ||
      entry.session !== null ||
      state.reopen !== null ||
      state.caveats.length > 0 ||
      read.length > 0,
  );
</script>

{#snippet link(target: { text: string; href: string | null })}
  {#if target.href}<a href={target.href}>{target.text}</a>{:else}<bdi>{target.text}</bdi>{/if}
{/snippet}

{#if shown}
  <h2>Research state</h2>
  <dl class="facts">
    {#if provenance.constraint}
      <dt>Standing constraint</dt>
      <dd>
        {#if provenance.accepted}
          A decision in effect, written by a fold's model, accepted by the user.
        {:else}
          A decision in effect on the user's words{#if quotes.length}; their words, as the platform
            found them in their message:{/if}
        {/if}
        {#each quotes as anchor, i (i)}<blockquote class="text">{anchor.quote}</blockquote>{/each}
      </dd>
    {/if}
    {#if provenance.basis !== null}
      <dt>Basis</dt>
      <dd>{provenance.basis}</dd>
    {/if}
    {#if provenance.anchors.length}
      <dt>Anchors</dt>
      <dd>
        <ul>
          {#each provenance.anchors as anchor, i (i)}<li>
              {@render link(anchor)}{#if anchor.quote !== null && !provenance.constraint}: “<bdi
                  class="text">{anchor.quote}</bdi
                >”{/if}
            </li>{/each}
        </ul>
      </dd>
    {/if}
    {#if standing}
      <dt>Standing</dt>
      <dd>
        <strong class="standing {standing.label}">{standing.word}</strong>: {standing.text}
        {#if standing.scope}<br />Within: <bdi>{standing.scope}</bdi>{/if}
        {#if standing.assumes.length}<br />Assuming: {#each standing.assumes as target, i (i)}{i > 0
              ? ', '
              : ''}{@render link(target)}{/each}{/if}
        {#if standing.by}<br />{standing.byWord}: {@render link(standing.by)}{/if}
      </dd>
    {/if}
    {#if entry.kind !== null && entry.type !== 'decision'}
      <dt>Kind</dt>
      <dd>{visible(statusText(entry.kind))}</dd>
    {/if}
    {#if entry.session !== null}
      <dt>Scope</dt>
      <dd>For session <bdi>{visible(entry.session)}</bdi> only</dd>
    {/if}
    {#if state.reopen !== null}
      <dt>Reopen condition</dt>
      <dd>
        <bdi class="text">{state.reopen}</bdi>
        {#if state.reopenFlagged}<strong class="flagged">: its flag is open</strong>{/if}
      </dd>
    {/if}
    {#if state.caveats.length}
      <dt>Caveats</dt>
      <dd>
        <ul>
          {#each state.caveats as caveat, i (i)}<li class="text">{caveat}</li>{/each}
        </ul>
      </dd>
    {/if}
    {#if read.length}
      <dt>Read</dt>
      <dd>
        <ul>
          {#each read as source, i (i)}<li>
              <bdi>{visible(source.source)}</bdi>
              <span class="muted">({visible(source.kind)})</span>: {readText(source)}
            </li>{/each}
        </ul>
      </dd>
    {/if}
  </dl>
{/if}

<style>
  .text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .standing.impossible {
    color: var(--failed, #c00);
  }
  .standing.claimed,
  .standing.disputed,
  .standing.flagged {
    color: var(--approximate, #b70);
  }
</style>
