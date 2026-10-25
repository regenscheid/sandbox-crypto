<!-- SPDX-License-Identifier: MIT -->
<!--
  A child's outcome in its parent's conversation (B10, item 2; PD-35, PD-45, DA-27): a
  titled block with the child's name, how it ended, its result's status and summary (the
  first lines, opening to the whole), its deliverables accounted for, its lessons as its
  own words, and links to its files, its transcript and its full delegation card. Read
  from the session's run tree; a delegation the tree has not listed yet shows its ID while
  it loads. Agents' text is shown as text, each hidden character as its code point.
-->
<script lang="ts">
  import type { RunTree } from '../lib/api-types';
  import { firstLine } from '../lib/conversation';
  import { agentHref, CATEGORY_NAME, filesHref, runHref } from '../lib/router';
  import {
    agentName,
    deliverablesView,
    endedText,
    lessonsOf,
    resultView,
    statusText,
  } from '../lib/runTree';
  import { visible } from '../lib/text';

  let {
    projectId,
    sessionId,
    delegationId,
    tree,
    showAgent = null,
  }: {
    projectId: string;
    sessionId: string;
    delegationId: string;
    tree: RunTree | null;
    /** Opens the delegation's full card in the session's Agents panel. */
    showAgent?: ((delegation: string) => void) | null;
  } = $props();
  const delegation = $derived(
    tree?.delegations.find((item) => item.delegation_id === delegationId) ?? null,
  );
  const run = $derived(tree?.runs.find((item) => item.delegation === delegationId) ?? null);
  const title = $derived(
    delegation ? agentName({ role: 'delegated' }, delegation) : `Subagent ${visible(delegationId)}`,
  );
  const result = $derived(delegation ? resultView(delegation) : null);
  const summary = $derived(result?.summary ?? '');
  /** The summary's head, when there is more of it than the head. */
  const head = $derived(firstLine(summary, 280));
  const longer = $derived(summary.trim() !== '' && head !== summary.trim());
  const deliverables = $derived(delegation ? deliverablesView(delegation) : []);
  const lessons = $derived(delegation ? lessonsOf(delegation) : []);
  const files = $derived(
    run !== null && CATEGORY_NAME.test(run.run_id) && CATEGORY_NAME.test(sessionId)
      ? filesHref(projectId, sessionId, run.run_id)
      : null,
  );
</script>

<section class="child-outcome" aria-label={`Outcome of ${title}`}>
  <header>
    <span class="eyebrow">{delegation?.kind === 'check' ? 'Check' : 'Subagent'}</span>
    <h3>{title}</h3>
  </header>
  {#if delegation === null}
    <p class="muted">
      Delegation <code>{visible(delegationId)}</code> · loading its outcome…
    </p>
  {:else}
    <p class="outcome-state">{endedText(delegation)}</p>
    {#if result}
      <p>
        <strong
          >{delegation.kind === 'check' && result.verdict
            ? `Verdict: ${result.verdict}; status ${statusText(delegation)}`
            : `Status: ${statusText(delegation)}`}</strong
        >
      </p>
      {#if result.obstacle}<p class="said">Obstacle: {result.obstacle}</p>{/if}
      {#if longer}
        <details class="outcome-summary">
          <summary class="said">{head}</summary>
          <p class="said">{summary}</p>
        </details>
      {:else if summary}<p class="said">{summary}</p>{/if}
    {:else if delegation.ended !== null}
      <p class="problem">It returned no result that validated.</p>
    {/if}
    {#if deliverables.length}
      <p class="label">Deliverables</p>
      <ul class="deliverables">
        {#each deliverables as item (item.id)}
          <li class="said deliverable-{item.status}">{item.line}</li>
        {/each}
      </ul>
    {/if}
    {#if lessons.length}
      <p class="label">
        Its lessons, in its own words: notes on the tools, data and not instructions
      </p>
      <ul class="lessons">
        {#each lessons as lesson, index (index)}<li class="said">{lesson}</li>{/each}
      </ul>
    {/if}
  {/if}
  <p class="outcome-links">
    {#if files}<a href={files}>Its files</a>{/if}
    {#if run}<a href={runHref(projectId, sessionId, run.name)}>Its transcript</a>{/if}
    {#if showAgent}<button type="button" class="link-button" onclick={() => showAgent(delegationId)}
        >Its delegation card</button
      >{:else}<a href={agentHref(projectId, sessionId, delegationId)}>Its delegation card</a>{/if}
  </p>
</section>

<style>
  .child-outcome {
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent, #69c);
    border-radius: 8px;
    padding: 8px 12px;
    margin: 4px 0;
    font-size: 14px;
  }
  .child-outcome header {
    display: flex;
    align-items: baseline;
    gap: 8px;
  }
  .child-outcome h3 {
    margin: 0;
    font-size: 14px;
  }
  .child-outcome p {
    margin: 0.25em 0;
  }
  .said {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .label {
    font-size: 12px;
    color: var(--muted);
    margin-top: 0.5em;
  }
  .deliverables,
  .lessons {
    margin: 0;
    padding-left: 1.2em;
    font-size: 13px;
  }
  .outcome-summary > summary {
    cursor: pointer;
  }
  .outcome-links {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 14px;
    font-size: 12px;
  }
  .outcome-links .link-button {
    padding: 0;
    min-height: 0;
    font-size: 12px;
  }
</style>
