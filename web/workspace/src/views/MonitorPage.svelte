<!-- SPDX-License-Identifier: MIT -->
<!-- The project's Monitor view (D103, built into the GUI since Grafana is avoided): calls
     by effect class and destination, approvals, holds and drift, spend and tokens, sandbox
     tiers and MCP canaries for the last 24 hours or 7 days. Every figure is the platform's
     count; names from records are shown through visible(). It polls while the page is
     visible, at the chat's cadence, and says which part is unavailable. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { CallRow, ProjectMonitor, SpendRow } from '../lib/api-types';
  import { browserState } from '../lib/browserState';
  import { formatCost, formatTime } from '../lib/format';
  import { costByRole, costSource, roleName } from '../lib/modelCost';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { ageText } from '../lib/questions';
  import { mcpHref, projectHref } from '../lib/router';
  import { visible } from '../lib/text';
  import PollStatus from './PollStatus.svelte';

  type Span = '24h' | '7d';
  /** The chat's cadence for what waits on the user (ChatPanel's permission requests). */
  const INTERVAL_MS = 2000;
  const SPANS: [Span, string][] = [
    ['24h', 'Last 24 hours'],
    ['7d', 'Last 7 days'],
  ];
  const KINDS: Record<string, string> = {
    mcp: 'Connector calls',
    network: 'Network requests',
    packages: 'Package installs',
    hold: 'Holds',
  };

  let { projectId }: { projectId: string } = $props();
  const scope = untrack(() => `monitor:${projectId}`);
  const kept = untrack(() => browserState.load(scope).draft ?? {});
  let span = $state<Span>(kept.span === '7d' ? '7d' : '24h');
  let view = $state<PollState<ProjectMonitor>>(initialPoll());
  $effect(() => {
    browserState.save(scope, { span });
  });
  $effect(() => {
    const project = projectId,
      chosen = span;
    view = initialPoll();
    return new Poller(
      (signal) => api.monitor(project, chosen, signal),
      (value) => {
        view = value;
      },
      { intervalMs: INTERVAL_MS },
    ).watch();
  });
  const data = $derived(view.data);
  const records = $derived(data?.worker?.records ?? null);
  const busiest = $derived(Math.max(1, ...(records?.calls ?? []).map(total)));

  function total(row: CallRow): number {
    return row.allowed + row.escalated + row.denied + row.held;
  }
  function destination(row: CallRow): string {
    if (row.server) return visible(row.server);
    if (row.host) return visible(row.host);
    return 'unknown';
  }
  function share(count: number): string {
    return `${(count / busiest) * 100}%`;
  }
  function cost(row: { cost: number | null; unpriced: number }): string {
    if (row.cost === null) return 'unknown';
    return row.unpriced
      ? `${formatCost(row.cost)} (+${row.unpriced} unpriced)`
      : formatCost(row.cost);
  }
  function wait(seconds: number | null | undefined): string {
    if (seconds === null || seconds === undefined) return '—';
    if (seconds < 90) return `${Math.round(seconds)} s`;
    if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
    return `${(seconds / 3600).toFixed(1)} h`;
  }
  function tier(value: number | null | undefined): string {
    return value === null || value === undefined ? 'not measured' : `tier ${value}`;
  }
  function modelName(row: SpendRow): string {
    return row.model ? visible(row.model) : 'unknown';
  }
  /** Open the service's Classification tab on the Connectors page. */
  function classify(server: string) {
    const mcpScope = `mcp:${projectId}`;
    const before = browserState.load(mcpScope).draft ?? {};
    browserState.save(mcpScope, { ...before, selected: server, tab: 'classification' });
  }
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
</nav>
<h1>Monitor</h1>
<div class="spans" role="group" aria-label="Window">
  {#each SPANS as [value, label] (value)}
    <button
      type="button"
      aria-pressed={span === value}
      class:active={span === value}
      onclick={() => (span = value)}>{label}</button
    >
  {/each}
</div>
<PollStatus phase={view.phase} hasData={data !== null} what="The monitor figures are" />

{#if data}
  <p class="muted">
    {formatTime(data.since)} to {formatTime(data.at)}{#if records}
      · {records.scanned} records read{#if records.sources.length}
        ({records.sources.join(' and ')}){/if}{/if}
  </p>
  {#if data.worker === null}
    <p class="problem unavailable" role="status">
      Calls, approvals, holds, drift and sandbox tiers are unavailable: {data.worker_unavailable ??
        'the worker did not answer'}.
    </p>
  {:else if records === null}
    <p class="problem unavailable" role="status">
      Calls, approvals, holds and drift are unavailable: {data.worker.unavailable ??
        'the event store cannot be read'}.
    </p>
  {/if}
  {#if records?.truncated}
    <p class="problem" role="status">
      The read stopped at its record limit: calls older than the oldest record read are not counted.
    </p>
  {/if}

  <section class="calls" aria-labelledby="monitor-calls">
    <h2 id="monitor-calls">Calls</h2>
    {#if records}
      {#if records.calls.length === 0}
        <p class="muted">No calls recorded in this window.</p>
      {:else}
        <table>
          <thead>
            <tr>
              <th>Server or host</th>
              <th>Class</th>
              <th>Allowed</th>
              <th>Escalated</th>
              <th>Denied</th>
              <th>Held</th>
              <th><span class="sr">Split</span></th>
            </tr>
          </thead>
          <tbody>
            {#each records.calls as row (`${row.server}/${row.host}/${row.effect}`)}
              <tr class="call-row">
                <td><code>{destination(row)}</code></td>
                <td>{visible(row.effect)}</td>
                <td>{row.allowed}</td>
                <td>{row.escalated}</td>
                <td>{row.denied}</td>
                <td>{row.held}</td>
                <td class="bar-cell">
                  <div class="bar" aria-hidden="true">
                    <span class="allowed" style:width={share(row.allowed)}></span><span
                      class="escalated"
                      style:width={share(row.escalated)}
                    ></span><span class="denied" style:width={share(row.denied)}></span><span
                      class="held"
                      style:width={share(row.held)}
                    ></span>
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        <p class="muted legend">
          <span class="key allowed"></span>allowed <span class="key escalated"></span>escalated
          <span class="key denied"></span>denied <span class="key held"></span>held. Research code's
          requests count only what the policy middleware records of them.
        </p>
      {/if}
    {/if}
    <p class="muted">{data.network_drops}</p>
  </section>

  <section aria-labelledby="monitor-approvals">
    <h2 id="monitor-approvals">Approvals</h2>
    {#if records}
      <table>
        <thead>
          <tr>
            <th>Kind</th>
            <th>Waiting</th>
            <th>Approved</th>
            <th>Denied</th>
            <th>Expired</th>
            <th>Median wait</th>
          </tr>
        </thead>
        <tbody>
          {#each records.approvals as item (item.kind)}
            <tr class="approval-row">
              <td>{KINDS[item.kind] ?? item.kind}</td>
              <td>{item.waiting}</td>
              <td>{item.approved}</td>
              <td>{item.denied}</td>
              <td>{item.expired}</td>
              <td>{wait(item.median_wait_seconds)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
    <p><a href={mcpHref(projectId)}>Grants</a> on the Connectors page.</p>
  </section>

  <section aria-labelledby="monitor-holds">
    <h2 id="monitor-holds">Holds and drift</h2>
    {#if records}
      {#if records.holds.length === 0}
        <p class="muted">No open holds.</p>
      {:else}
        <ul class="holds">
          {#each records.holds as hold (hold.rule)}
            <li>
              <code>{visible(hold.rule)}</code>: {hold.count} open{#if hold.sessions.length},
                sessions
                {hold.sessions.join(', ')}{/if}{#if hold.oldest_at}, the oldest since {formatTime(
                  hold.oldest_at,
                )}{/if}
            </li>
          {/each}
        </ul>
      {/if}
      {#if records.drift.length === 0}
        <p class="muted">No server's tool list has drifted unaccepted.</p>
      {:else}
        <ul class="drift">
          {#each records.drift as item (item.server)}
            <li>
              <a href={mcpHref(projectId)} onclick={() => classify(item.server)}
                ><code>{visible(item.server)}</code></a
              >: its tool list drifted{#if item.at}
                {formatTime(item.at)}{/if}; its non-read calls ask until it is accepted again in its
              Classification tab.
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  </section>

  <section aria-labelledby="monitor-spend">
    <h2 id="monitor-spend">Spend and tokens</h2>
    <p class="spend-total">
      This window: <strong>{cost(data.spend)}</strong> · {data.spend.tokens.toLocaleString()} tokens in
      {data.spend.calls} calls
    </p>
    {#if data.spend.by_role.length}
      <table>
        <thead><tr><th>Role</th><th>Cost</th><th>Tokens</th><th>Calls</th></tr></thead>
        <tbody>
          {#each data.spend.by_role as row (row.role)}
            <tr class="role-row">
              <td>{roleName(row.role ?? '')}</td>
              <td>{cost(row)}</td>
              <td>{row.tokens.toLocaleString()}</td>
              <td>{row.calls}</td>
            </tr>
          {/each}
        </tbody>
      </table>
      <table>
        <thead><tr><th>Model</th><th>Cost</th><th>Tokens</th><th>Calls</th></tr></thead>
        <tbody>
          {#each data.spend.by_model as row (row.model ?? '')}
            <tr class="model-row">
              <td>{modelName(row)}</td>
              <td>{cost(row)}</td>
              <td>{row.tokens.toLocaleString()}</td>
              <td>{row.calls}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {:else}
      <p class="muted">No model calls recorded in this window.</p>
    {/if}
    {#if data.model_cost.total !== null}
      <p class="project-cost">
        The project: {formatCost(data.model_cost.last_day ?? 0)} in the last 24 hours, {formatCost(
          data.model_cost.total,
        )} in all, {costSource(data.model_cost)}.
      </p>
      {#each costByRole(data.model_cost) as line (line)}
        <p class="muted">{line}</p>
      {/each}
    {:else}
      <p class="muted project-cost">The project's cost: unknown, no call was priced.</p>
    {/if}
  </section>

  <section aria-labelledby="monitor-tiers">
    <h2 id="monitor-tiers">Sandbox tiers</h2>
    {#if data.worker}
      {#if data.worker.environments.length === 0}
        <p class="muted">No environments.</p>
      {:else}
        <table>
          <thead>
            <tr
              ><th>Environment</th><th>Kind</th><th>State</th><th>Declared</th><th>In effect</th
              ></tr
            >
          </thead>
          <tbody>
            {#each data.worker.environments as item (item.environment_id)}
              <tr class="tier-row">
                <td><code>{item.environment_id}</code> <span class="muted">{item.profile}</span></td
                >
                <td>{item.kind === 'job' ? `job ${item.job_id ?? ''}` : 'session'}</td>
                <td>{item.state}</td>
                <td>{tier(item.declared_tier)}</td>
                <td>{tier(item.tier)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    {/if}
  </section>

  <section aria-labelledby="monitor-canaries">
    <h2 id="monitor-canaries">Canaries</h2>
    {#if data.canaries.length === 0}
      <p class="muted">No MCP services are configured for this project.</p>
    {:else}
      <ul class="canaries">
        {#each data.canaries as item (item.server)}
          <li>
            <code>{visible(item.server)}</code>:
            {#if item.canary}
              {item.canary.status}
              {ageText(item.canary.at, Date.parse(data.at))}{#if item.canary.tool_count !== null}
                · {item.canary.tool_count} tools{/if} · {item.canary.elapsed_ms} ms
            {:else}
              none recorded
            {/if}
          </li>
        {/each}
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
  .bar-cell {
    width: 30%;
    min-width: 120px;
  }
  .bar {
    display: flex;
    height: 10px;
    background: var(--panel);
    border-radius: 3px;
    overflow: hidden;
  }
  .allowed {
    background: var(--ok);
  }
  .escalated {
    background: var(--approximate);
  }
  .denied {
    background: var(--failed);
  }
  .held {
    background: var(--muted);
  }
  .key {
    display: inline-block;
    width: 10px;
    height: 10px;
    margin: 0 4px 0 8px;
    border-radius: 2px;
  }
  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }
</style>
