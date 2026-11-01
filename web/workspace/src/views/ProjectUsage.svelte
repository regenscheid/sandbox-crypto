<!-- SPDX-License-Identifier: MIT -->
<!-- The project's Usage page (D102): what its sessions spent in a recent window or over all
     of its life, the check-ins and loop detections in that window, and the optional local
     cost limit and notices. Accounting only: the model gateway's budget is the one spending
     control. The Monitor page keeps the window's spend by role beside the platform's other
     counts; each session's own Usage tab goes by agent and soft cap. It reads while visible,
     slowly, since the figures move slowly. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { ProjectUsageView } from '../lib/api-types';
  import { browserState } from '../lib/browserState';
  import { formatTime } from '../lib/format';
  import { roleName } from '../lib/modelCost';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { monitorHref, projectHref, sessionUsageHref } from '../lib/router';
  import { visible } from '../lib/text';
  import {
    answeredText,
    checkInSummary,
    limitText,
    loopLines,
    modelText,
    spendText,
  } from '../lib/usage';
  import PollStatus from './PollStatus.svelte';

  type Window = '24h' | '7d' | 'all';
  const INTERVAL_MS = 10_000;
  const SPANS: [Window, string][] = [
    ['24h', 'Last 24 hours'],
    ['7d', 'Last 7 days'],
    ['all', 'All time'],
  ];

  let { projectId }: { projectId: string } = $props();
  const scope = untrack(() => `usage-page:${projectId}`);
  const kept = untrack(() => browserState.load(scope).draft ?? {});
  let span = $state<Window>(kept.span === '7d' || kept.span === 'all' ? kept.span : '24h');
  let view = $state<PollState<ProjectUsageView>>(initialPoll());
  $effect(() => {
    browserState.save(scope, { span });
  });
  $effect(() => {
    const project = projectId,
      chosen = span;
    view = initialPoll();
    return new Poller(
      (signal) => api.projectUsage(project, chosen, signal),
      (value) => {
        view = value;
      },
      { intervalMs: INTERVAL_MS },
    ).watch();
  });
  const data = $derived(view.data);
  const limit = $derived(data ? limitText(data.daily_cost_limit, data.last_day_cost) : null);
  const label = $derived(SPANS.find(([value]) => value === span)?.[1].toLowerCase() ?? '');
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
</nav>
<h1>Usage</h1>
<div class="spans" role="group" aria-label="Window">
  {#each SPANS as [value, text] (value)}
    <button
      type="button"
      aria-pressed={span === value}
      class:active={span === value}
      onclick={() => (span = value)}>{text}</button
    >
  {/each}
</div>
<PollStatus phase={view.phase} hasData={data !== null} what="The usage figures are" />

{#if data}
  <p class="muted">
    {data.since ? `${formatTime(data.since)} to ` : 'Up to '}{formatTime(data.at)}. Accounting only:
    nothing here stops spending. The model gateway's budget does.
  </p>

  <section aria-labelledby="usage-spend">
    <h2 id="usage-spend">Spend and tokens</h2>
    <p class="window-total">
      {label.charAt(0).toUpperCase() + label.slice(1)}:
      <strong>{spendText(data.total.cost, data.total.unpriced)}</strong>
      · {data.total.tokens.toLocaleString()} tokens in {data.total.calls} calls
    </p>
    {#if span !== 'all'}
      <p class="muted all-time">
        Over the project's whole life: {spendText(data.all_time.cost, data.all_time.unpriced)} ·
        {data.all_time.tokens.toLocaleString()} tokens in {data.all_time.calls} calls.
      </p>
    {/if}
    {#if data.total.by_role.length}
      <ul class="roles">
        {#each data.total.by_role as row (row.role)}
          <li class="role-row">
            {roleName(row.role)}: {spendText(row.cost, row.unpriced)} · {row.tokens.toLocaleString()}
            tokens · {row.calls} calls
          </li>
        {/each}
      </ul>
    {:else}
      <p class="muted">No model calls recorded {span === 'all' ? '' : 'in this window'}.</p>
    {/if}
    {#if data.unassigned.calls}
      <p class="muted unassigned">
        {spendText(data.unassigned.cost, data.unassigned.unpriced)} of it was for calls no session is
        recorded for (the skills' embeddings and catalog passes); only the totals count it.
      </p>
    {/if}
    {#if limit}<p class="limit">{limit}</p>{/if}
    {#each data.notices as notice (notice.threshold)}
      <p class="muted notice">
        Notice at ${notice.threshold}: {notice.crossed_at
          ? `crossed ${formatTime(notice.crossed_at)}${notice.spent !== null ? ` with ${spendText(notice.spent, 0)} recorded` : ''}`
          : 'not crossed yet'}. A notice never stops anything.
      </p>
    {/each}
    <p class="muted">
      By role and model, beside the platform's other counts, see the <a
        href={monitorHref(projectId)}>Monitor</a
      >.
    </p>
  </section>

  <section aria-labelledby="usage-sessions">
    <h2 id="usage-sessions">Sessions</h2>
    {#if data.sessions.length === 0}
      <p class="muted">No session used a model {span === 'all' ? '' : 'in this window'}.</p>
    {:else}
      <table>
        <thead
          ><tr
            ><th>Session</th><th>Cost</th><th>Tokens</th><th>Calls</th><th>Check-ins</th><th
              >Loops</th
            ><th>Waiting</th></tr
          ></thead
        >
        <tbody>
          {#each data.sessions as row (row.session)}
            <tr class="session-row">
              <td><a href={sessionUsageHref(projectId, row.session)}>{visible(row.session)}</a></td>
              <td>{spendText(row.cost, row.unpriced)}</td>
              <td>{row.tokens.toLocaleString()}</td>
              <td>{row.calls}</td>
              <td>{row.check_ins}</td>
              <td>{row.loops}</td>
              <td>{row.waiting}</td>
            </tr>
          {/each}
        </tbody>
      </table>
      {#if data.more_sessions}
        <p class="muted">
          {data.more_sessions} more sessions are not listed; the totals count them.
        </p>
      {/if}
      <p class="muted">
        Each session leads to its Usage tab: its agents against their soft caps and its check-ins.
      </p>
    {/if}
  </section>

  <section aria-labelledby="usage-models">
    <h2 id="usage-models">Models</h2>
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
  </section>

  <section aria-labelledby="usage-check-ins">
    <h2 id="usage-check-ins">Check-ins and loop detections</h2>
    <p class="check-in-summary">{checkInSummary(data.counts)}</p>
    {#if answeredText(data.counts)}
      <p class="muted answered">Answered: {answeredText(data.counts)}.</p>
    {/if}
    {#if loopLines(data.counts).length}
      <ul class="loops">
        {#each loopLines(data.counts) as line (line)}<li>{line}</li>{/each}
      </ul>
    {/if}
  </section>
{/if}

<style>
  .spans {
    display: flex;
    gap: 6px;
    margin: 0 0 12px;
  }
  .spans button.active {
    font-weight: 600;
    border-color: var(--link);
  }
  section {
    margin: 0 0 20px;
  }
  ul {
    list-style: none;
    margin: 0 0 0.5rem;
    padding: 0;
  }
</style>
