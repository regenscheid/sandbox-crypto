<!-- SPDX-License-Identifier: MIT -->
<!-- The session's Usage tab (D102): what the session has spent, by agent and by model,
     each agent against its soft caps, and the check-ins those caps and the loop
     heuristics raised, with how each was answered. Read-only: a soft cap is a check-in,
     never a stop, and only the model gateway's budget (or a configured local project
     limit) stops spending. It reads while the tab is shown. The monitor page keeps a
     window's spend by role and model beside the platform's other counts. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { ResourceRequest, ResourceRequestPage, SessionUsageView } from '../lib/api-types';
  import { browserState } from '../lib/browserState';
  import { formatTime } from '../lib/format';
  import { get } from '../lib/http';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { messageHref, usageHref } from '../lib/router';
  import { roleName } from '../lib/modelCost';
  import { visible } from '../lib/text';
  import {
    agentDepth,
    agentTitle,
    answeredText,
    CAP_NAMES,
    capShare,
    capState,
    capText,
    checkInOutcome,
    checkInReason,
    checkInSummary,
    checkInUse,
    joinCheckIns,
    limitText,
    loopLines,
    modelText,
    orderedAgents,
    spendText,
    strainedCount,
    strainText,
    type AgentOrder,
  } from '../lib/usage';
  import ErrorMessage from './ErrorMessage.svelte';
  import PollStatus from './PollStatus.svelte';

  let {
    projectId,
    sessionId,
    active = true,
  }: { projectId: string; sessionId: string; active?: boolean } = $props();
  /** Slow, since the figures move slowly and the read is a scan of the model-call records. */
  const INTERVAL_MS = 10_000;
  const scope = untrack(() => `usage:${projectId}:${sessionId}`);
  const kept = untrack(() => browserState.load(scope).draft ?? {});
  let order = $state<AgentOrder>(
    kept.order === 'cost' || kept.order === 'tokens' ? kept.order : 'tree',
  );
  let view = $state<PollState<SessionUsageView>>(initialPoll());
  let older = $state<ResourceRequest[]>([]);
  let nextAfter = $state<string | null | undefined>(undefined);
  let loadingOlder = $state(false);
  let olderError = $state<unknown>(null);
  $effect(() => {
    browserState.save(scope, { order });
  });
  $effect(() => {
    if (!active) return;
    const project = projectId,
      session = sessionId;
    return new Poller(
      (signal) => api.sessionUsage(project, session, signal),
      (value) => {
        view = value;
      },
      { intervalMs: INTERVAL_MS },
    ).watch();
  });
  const data = $derived(view.data);
  const agents = $derived(data ? orderedAgents(data.agents, order) : []);
  const strain = $derived(data ? strainedCount(data) : { near: 0, reached: 0 });
  const requests = $derived(data ? joinCheckIns(data.check_ins.requests, older) : []);
  const more = $derived(nextAfter === undefined ? (data?.check_ins.next_after ?? null) : nextAfter);
  const limit = $derived(data ? limitText(data.daily_cost_limit, null) : null);
  const ORDERS: [AgentOrder, string][] = [
    ['tree', 'As they began'],
    ['cost', 'Costliest first'],
    ['tokens', 'Most tokens first'],
  ];

  async function loadOlder() {
    if (!more || loadingOlder) return;
    loadingOlder = true;
    olderError = null;
    try {
      const page = await get<ResourceRequestPage>(
        `/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}` +
          `/recovery/resources?after=${encodeURIComponent(more)}`,
      );
      older = joinCheckIns(older, page.requests);
      nextAfter = page.next_after;
    } catch (caught) {
      olderError = caught;
    } finally {
      loadingOlder = false;
    }
  }
</script>

<section aria-label="Usage" class="usage-panel">
  <p class="muted intro">
    What this session has spent and what its agents have used against their soft caps. A soft cap is
    a check-in, not a stop: the agent's parent (or, for the root, the root itself and then you)
    decides whether it goes on. Only the model gateway's budget stops spending.
  </p>
  <PollStatus phase={view.phase} hasData={data !== null} what="The usage figures are" />
  {#if data}
    <h2>Spend</h2>
    <p class="totals">
      <strong class="session-cost">{spendText(data.total.cost, data.total.unpriced)}</strong>
      · {data.total.tokens.toLocaleString()} tokens in {data.total.calls} calls
    </p>
    {#if data.total.calls === 0}
      <p class="muted">No model calls recorded in this session yet.</p>
    {/if}
    {#if data.total.by_role.length > 1}
      <ul class="roles">
        {#each data.total.by_role as row (row.role)}
          <li class="role-row">
            {roleName(row.role)}: {spendText(row.cost, row.unpriced)} · {row.tokens.toLocaleString()}
            tokens · {row.calls} calls
          </li>
        {/each}
      </ul>
    {/if}
    {#if limit}<p class="muted limit">{limit}</p>{/if}
    {#each data.notices as notice (notice.threshold)}
      <p class="muted notice">
        Project notice at ${notice.threshold}: {notice.crossed_at
          ? `crossed ${formatTime(notice.crossed_at)}`
          : 'not crossed yet'}.
      </p>
    {/each}
    <p class="muted">
      The project's figures for a recent window are on the <a href={usageHref(projectId)}
        >project's Usage page</a
      >.
    </p>

    <h2>Agents</h2>
    {#if data.agents.length === 0}
      <p class="muted">No agent has used a model in this session yet.</p>
    {:else}
      {#if strainText(strain)}
        <p class="strain" role="status">{strainText(strain)}</p>
      {/if}
      <div class="orders" role="group" aria-label="Order">
        {#each ORDERS as [value, label] (value)}
          <button
            type="button"
            aria-pressed={order === value}
            class:active={order === value}
            onclick={() => (order = value)}>{label}</button
          >
        {/each}
      </div>
      <ul class="agents">
        {#each agents as agent (agent.key)}
          <li
            class="agent"
            data-state={agent.state ?? ''}
            style:margin-left={order === 'tree'
              ? `${Math.min(agentDepth(data.agents, agent), 4) * 0.9}rem`
              : undefined}
          >
            <div class="agent-head">
              <strong class="agent-title">{agentTitle(agent)}</strong>
              {#if agent.state}<span class="muted state"
                  >{agent.state}{agent.reason ? `, ${visible(agent.reason)}` : ''}</span
                >{/if}
            </div>
            <p class="agent-spend">
              {spendText(agent.cost, agent.unpriced)} · {agent.tokens.toLocaleString()} tokens ·
              {agent.calls} calls{#if agent.runs.length > 1}
                · {agent.runs.length} attempts{/if}
            </p>
            {#if agent.caps.length}
              <ul class="caps">
                {#each agent.caps as cap (cap.resource)}
                  <li class="cap" data-state={capState(cap)}>
                    <span class="cap-name">{CAP_NAMES[cap.resource]}</span>
                    <span class="bar" aria-hidden="true"
                      ><span class="fill" style:width={`${capShare(cap)}%`}></span></span
                    >
                    <span class="cap-text">{capText(cap)}</span>
                  </li>
                {/each}
              </ul>
            {/if}
            {#if agent.lines.length}
              <table class="lines">
                <thead
                  ><tr><th>Role</th><th>Model</th><th>Cost</th><th>Tokens</th><th>Calls</th></tr
                  ></thead
                >
                <tbody>
                  {#each agent.lines as line (`${line.role}/${line.model}`)}
                    <tr class="line">
                      <td>{roleName(line.role)}</td>
                      <td>{modelText(line.model)}</td>
                      <td>{spendText(line.cost, line.unpriced)}</td>
                      <td>{line.tokens.toLocaleString()}</td>
                      <td>{line.calls}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            {/if}
          </li>
        {/each}
      </ul>
      {#if data.more_agents}
        <p class="muted">{data.more_agents} more agents are not listed; the totals count them.</p>
      {/if}
      <p class="muted">
        A work's token cap counts its own model calls and folds. Its cost cap also counts the calls
        made for it, such as the reviewer's, and leaves out any call the gateway did not price.
        Active time is what the agent was observed working, not waiting for a decision.
      </p>
    {/if}

    <h2>Models</h2>
    {#if data.by_model.length === 0}
      <p class="muted">No model calls recorded.</p>
    {:else}
      <table>
        <thead><tr><th>Model</th><th>Cost</th><th>Tokens</th><th>Calls</th></tr></thead>
        <tbody>
          {#each data.by_model as row (row.model ?? '')}
            <tr class="model-row">
              <td>{modelText(row.model)}</td>
              <td>{spendText(row.cost, row.unpriced)}</td>
              <td>{row.tokens.toLocaleString()}</td>
              <td>{row.calls}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}

    <h2>Check-ins</h2>
    <p class="check-in-summary">{checkInSummary(data.counts)}</p>
    {#if answeredText(data.counts)}
      <p class="muted answered">Answered: {answeredText(data.counts)}.</p>
    {/if}
    {#if loopLines(data.counts).length}
      <h3>Loop detections</h3>
      <ul class="loops">
        {#each loopLines(data.counts) as line (line)}<li>{line}</li>{/each}
      </ul>
    {/if}
    {#if requests.length}
      <ul class="check-ins">
        {#each requests as item (item.request_id)}
          <li class="check-in" data-resource={item.resource}>
            <div>
              <strong>{checkInReason(item)}</strong>
              <span class="muted"
                >· {formatTime(new Date(item.created_at * 1000).toISOString())}</span
              >
            </div>
            <div class="muted">{visible(item.task || 'Research branch')}</div>
            <div>{checkInUse(item)}</div>
            {#if item.summary?.detail}<div class="muted detail">
                {visible(item.summary.detail)}
              </div>{/if}
            <div class="outcome">
              {checkInOutcome(item)}{#if item.decision?.request.reason}:
                {visible(item.decision.request.reason)}{/if}
            </div>
            {#if item.checkpoint_message_id}<a
                href={messageHref(projectId, item.checkpoint_message_id)}>Read checkpoint</a
              >{/if}
          </li>
        {/each}
      </ul>
      {#if olderError}<ErrorMessage error={olderError} />{/if}
      {#if more}<button type="button" disabled={loadingOlder} onclick={loadOlder}
          >Older check-ins</button
        >{/if}
    {/if}
  {/if}
</section>

<style>
  h2 {
    margin: 1.2rem 0 0.4rem;
    font-size: 1.05rem;
  }
  h3 {
    margin: 0.8rem 0 0.3rem;
    font-size: 0.95rem;
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .totals {
    margin: 0.2rem 0;
  }
  .orders {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 0.4rem 0 0.6rem;
  }
  .orders button.active {
    font-weight: 600;
    border-color: var(--link);
  }
  .agent,
  .check-in {
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.55rem 0.7rem;
    margin: 0 0 0.5rem;
  }
  .agent-head {
    display: flex;
    flex-wrap: wrap;
    gap: 0.2rem 0.6rem;
    align-items: baseline;
  }
  .agent-spend {
    margin: 0.25rem 0;
  }
  .caps {
    display: grid;
    gap: 0.25rem;
    margin: 0.35rem 0;
  }
  .cap {
    display: grid;
    grid-template-columns: 6.5rem minmax(3rem, 1fr);
    gap: 0 0.5rem;
    align-items: center;
    font-size: 0.9em;
  }
  .cap-text {
    grid-column: 1 / -1;
    color: var(--muted);
  }
  .bar {
    display: block;
    height: 8px;
    background: var(--panel);
    border-radius: 3px;
    overflow: hidden;
  }
  .fill {
    display: block;
    height: 100%;
    background: var(--ok);
  }
  .cap[data-state='near'] .fill {
    background: var(--approximate);
  }
  .cap[data-state='reached'] .fill {
    background: var(--failed);
  }
  .cap[data-state='off'] .fill {
    background: var(--muted);
  }
  .strain {
    color: var(--approximate);
    margin: 0.3rem 0;
  }
  .lines {
    margin-top: 0.4rem;
    font-size: 0.85em;
  }
  .outcome {
    margin-top: 0.2rem;
  }
</style>
