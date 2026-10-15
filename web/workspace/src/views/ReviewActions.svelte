<!-- SPDX-License-Identifier: MIT -->
<!-- A review window's actions (docs/specs/consistency-review.md, CR-2 as amended for lab
     40, 41): a compact table for each run, a delegated run's under its delegation, its
     calls that change something in order, each
     with its tool, target, outcome, a refused or failed one marked, and what it made,
     leading there where the workspace has a page; then the blocks each document revision
     changed. Agents' arguments are shown as text, each character a person may not see as
     its code point. Only props, so a test renders it on the server. -->
<script lang="ts">
  import {
    blockLink,
    madeLink,
    marked,
    runLink,
    targetText,
    type WindowActions,
  } from '../lib/reviewActions';
  import { visible } from '../lib/text';

  let {
    projectId,
    sessionId,
    actions,
  }: { projectId: string; sessionId: string; actions: WindowActions } = $props();
</script>

{#snippet link(target: { text: string; href: string | null })}
  {#if target.href}<a href={target.href}>{target.text}</a>{:else}<bdi>{target.text}</bdi>{/if}
{/snippet}

<section class="actions" aria-label="What the runs did">
  <h4>What the runs did</h4>
  {#if actions.total > actions.shown}
    <p class="muted">The first {actions.shown} of the window's {actions.total} calls are listed.</p>
  {/if}
  {#each actions.runs as group, index (index)}
    {#if group.delegation !== null}
      <p>
        Delegated run {@render link(runLink(projectId, sessionId, group.run))}, of delegation
        <code>{visible(group.delegation)}</code>
      </p>
    {:else}
      <p>Run {@render link(runLink(projectId, sessionId, group.run))}</p>
    {/if}
    <table>
      <thead><tr><th>#</th><th>Tool</th><th>Target</th><th>Outcome</th><th>Made</th></tr></thead>
      <tbody>
        {#each group.actions as action, n (n)}
          <tr class:marked={marked(action.outcome)}>
            <td>{n + 1}</td>
            <td><code>{visible(action.tool)}</code></td>
            <td><bdi>{targetText(action)}</bdi></td>
            <td
              >{#if marked(action.outcome)}<strong class="problem">{visible(action.outcome)}</strong
                >{:else}{visible(action.outcome)}{/if}</td
            >
            <td>
              {#each action.made as made, m (m)}{m > 0 ? '; ' : ''}{@render link(
                  madeLink(projectId, sessionId, made),
                )}{:else}<span class="muted">nothing</span>{/each}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <p class="muted">No run changed anything in this window.</p>
  {/each}
  {#if actions.documents.length}
    <p class="label">The blocks each document revision changed</p>
    <ul class="links">
      {#each actions.documents as change, index (index)}
        <li>
          <bdi>{visible(change.document)}</bdi>{change.revision === null
            ? ''
            : ` r${change.revision}`}{change.since === null
            ? ', its first revision'
            : ` since r${change.since}`}:
          {#each change.changed as block, b (b)}{b > 0 ? ', ' : ''}{@render link(
              blockLink(projectId, change, block),
            )}{:else}no block changed{/each}{change.changedTotal > change.changed.length
            ? ` and ${change.changedTotal - change.changed.length} more`
            : ''}{#if change.removed.length}; removed {change.removed
              .map(visible)
              .join(', ')}{change.removedTotal > change.removed.length
              ? ` and ${change.removedTotal - change.removed.length} more`
              : ''}{/if}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  table {
    border-collapse: collapse;
    font-size: 0.9em;
    margin-bottom: 0.6em;
  }
  th,
  td {
    border-bottom: 1px solid var(--border, #9996);
    padding: 0.15em 0.4em;
    text-align: left;
    vertical-align: top;
    overflow-wrap: anywhere;
  }
  tr.marked {
    background: color-mix(in srgb, var(--failed, #c00) 8%, transparent);
  }
  .label {
    font-size: 0.9em;
  }
</style>
