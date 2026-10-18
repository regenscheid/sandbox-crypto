<!-- SPDX-License-Identifier: MIT -->
<!-- Configured identity and historical observations only. Viewing this page never calls the proxy. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { McpInventory } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { projectHref } from '../lib/router';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';
  import PaneResizer from './PaneResizer.svelte';

  let { projectId }: { projectId: string } = $props();
  let shown = $state<McpInventory | null>(null);
  let error = $state<unknown>(null);
  let selected = $state<string | null>(null);
  let search = $state('');
  let filter = $state('all');
  let tab = $state<'overview' | 'tools' | 'policy' | 'observations'>('overview');
  let width = $state(460);
  let viewportWidth = $state(window.innerWidth);
  const maxWidth = $derived(Math.max(320, Math.floor(viewportWidth / 2)));
  const services = $derived(shown?.services ?? []);
  const filtered = $derived(
    services.filter(
      (service) =>
        (filter === 'all' ||
          (filter === 'unobserved' && service.observation === null) ||
          service.observation?.status === filter) &&
        `${service.name} ${service.purpose}`
          .toLocaleLowerCase()
          .includes(search.trim().toLocaleLowerCase()),
    ),
  );
  const current = $derived(
    filtered.find((service) => service.name === selected) ?? filtered[0] ?? null,
  );

  async function load() {
    try {
      const found = await api.mcpServices(projectId);
      shown = found;
      if (selected === null || !found.services.some((service) => service.name === selected))
        selected = found.services[0]?.name ?? null;
      error = null;
    } catch (caught) {
      error = caught;
    }
  }
  void load();
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
<h1>MCP services</h1>
<p class="muted">
  Configured services and observations from a bounded sample of recorded runs. The most recent
  result shown for a service may not be its latest run. This page does not test current
  availability.
</p>
{#if error}<ErrorMessage {error} /><button type="button" onclick={() => void load()}
    >Retry inventory</button
  >
{:else if shown === null}<p class="muted">Loading…</p>
{:else}
  <div class="mcp-filters">
    <label
      >Search services <input
        type="search"
        bind:value={search}
        placeholder="Name or purpose"
      /></label
    ><label
      >Observation <select value={filter} onchange={(event) => (filter = event.currentTarget.value)}
        ><option value="all">All</option><option value="listed">Listed in a run</option><option
          value="unavailable">Listing failed</option
        ><option value="unobserved">No observation</option></select
      ></label
    ><button type="button" onclick={() => void load()}>Refresh records</button>
  </div>
  <p class="muted">
    {filtered.length} of {services.length} configured services. {shown.observation_scope} Inspected {shown.inspected_runs}
    run records; skipped {shown.skipped_runs}.
  </p>
  <div class="mcp-columns" style:--detail-width={`${width}px`}>
    <section class="mcp-list" aria-label="Configured MCP services">
      {#each filtered as service (service.name)}
        <button
          type="button"
          class="mcp-row"
          class:selected={current?.name === service.name}
          aria-pressed={current?.name === service.name}
          onclick={() => {
            selected = service.name;
            tab = 'overview';
          }}
        >
          <span class="row-top"
            ><strong>{visible(service.name)}</strong><span class="state"
              >{service.observation?.status === 'listed'
                ? 'Listed before'
                : service.observation?.status === 'unavailable'
                  ? 'Listing failed'
                  : 'Unobserved'}</span
            ></span
          >
          <span>{service.purpose ? visible(service.purpose) : 'No purpose configured'}</span>
          {#if service.observation}<small class="muted"
              >Observed {formatTime(service.observation.at)}</small
            >{/if}
        </button>
      {:else}<p class="muted">
          {services.length
            ? 'No services match these filters.'
            : 'No MCP services are configured for this project in this workspace.'}
        </p>{/each}
    </section>
    <PaneResizer bind:width {maxWidth} label="Resize MCP service details" />
    <aside class="mcp-detail" aria-label="MCP service details">
      {#if current}
        <span class="eyebrow">Configured service</span>
        <h2>{visible(current.name)}</h2>
        <p>{current.purpose ? visible(current.purpose) : 'No purpose configured.'}</p>
        <div class="detail-tabs" role="tablist" aria-label="Service details">
          {#each ['overview', 'tools', 'policy', 'observations'] as choice}
            <button
              type="button"
              role="tab"
              aria-selected={tab === choice}
              class:active={tab === choice}
              onclick={() => (tab = choice as typeof tab)}
              >{choice.slice(0, 1).toUpperCase() + choice.slice(1)}</button
            >
          {/each}
        </div>
        {#if tab === 'overview'}
          <dl class="facts">
            <dt>Configuration</dt>
            <dd>Configured for this project</dd>
            <dt>Purpose</dt>
            <dd>{current.purpose ? visible(current.purpose) : 'Not specified'}</dd>
            <dt>Sampled observation</dt>
            <dd>
              {current.observation
                ? `${current.observation.status === 'listed' ? 'Listed' : 'Listing failed'} in a run at ${formatTime(current.observation.at)}`
                : 'None in the bounded run sample'}
            </dd>
          </dl>
        {:else if tab === 'tools'}
          {#if current.observation?.status === 'listed'}<p>
              A recorded run listed {current.observation.listed_tools} tools. This record does not retain
              service-scoped tool names, descriptions or schemas.
            </p>{:else}<p>No successful tool listing is present in the bounded run sample.</p>{/if}
          <p class="muted">
            Viewing tool information here never invokes a service or grants tool access.
          </p>
        {:else if tab === 'policy'}
          <p>Project policy revision: <code>{visible(shown.policy_revision)}</code>.</p>
          <p class="muted">
            The workspace does not yet project service-specific policy or approval rules.
            Configuration and past listings are not proof that a future call will be permitted.
          </p>
        {:else}
          {#if current.observation}<dl class="facts">
              <dt>Recorded result</dt>
              <dd>
                {current.observation.status === 'listed'
                  ? 'Tool listing succeeded'
                  : 'Tool listing unavailable'}
              </dd>
              <dt>Observed at</dt>
              <dd>{formatTime(current.observation.at)}</dd>
              <dt>Session</dt>
              <dd><code>{visible(current.observation.session)}</code></dd>
              <dt>Run</dt>
              <dd><code>{visible(current.observation.run)}</code></dd>
              {#if current.observation.listed_tools !== null}<dt>Listed tools</dt>
                <dd>{current.observation.listed_tools}</dd>{/if}
            </dl>{:else}<p>
              No observation of this service is present in the bounded run sample.
            </p>{/if}
          <p class="muted">
            This is historical evidence, not a live availability check. Failure messages are
            withheld because they are untrusted remote text.
          </p>
        {/if}
      {:else}<p class="muted">Select a configured service for details.</p>{/if}
    </aside>
  </div>
{/if}

<style>
  .mcp-filters {
    display: flex;
    flex-wrap: wrap;
    align-items: end;
    gap: 0.7rem;
    padding: 0.8rem;
    border: 1px solid var(--border, #ccc);
    border-radius: 0.5rem;
  }
  .mcp-filters label {
    display: grid;
    gap: 0.2rem;
    font-size: 0.85rem;
  }
  .mcp-filters input,
  .mcp-filters select {
    padding: 0.4rem;
  }
  .mcp-columns {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 8px minmax(320px, var(--detail-width));
    min-height: 30rem;
    border: 1px solid var(--border, #ccc);
    border-radius: 0.5rem;
    overflow: hidden;
  }
  .mcp-list,
  .mcp-detail {
    overflow: auto;
    max-height: 70vh;
  }
  .mcp-detail {
    padding: 1rem;
    border-left: 1px solid var(--border, #ccc);
  }
  .mcp-row {
    width: 100%;
    display: grid;
    gap: 0.35rem;
    text-align: left;
    background: transparent;
    border: 0;
    border-bottom: 1px solid var(--border, #ccc);
    padding: 0.9rem;
    cursor: pointer;
  }
  .mcp-row:hover,
  .mcp-row.selected {
    background: var(--surface-raised, #f1f4f3);
  }
  .row-top {
    display: flex;
    justify-content: space-between;
    gap: 0.6rem;
  }
  .state {
    font-size: 0.8rem;
    white-space: nowrap;
  }
  .detail-tabs {
    display: flex;
    flex-wrap: wrap;
    border-bottom: 1px solid var(--border, #ccc);
    margin: 1rem 0;
  }
  .detail-tabs button {
    border: 0;
    border-bottom: 2px solid transparent;
    padding: 0.5rem;
    background: transparent;
    cursor: pointer;
  }
  .detail-tabs button.active {
    border-bottom-color: currentColor;
    font-weight: 700;
  }
  @media (max-width: 800px) {
    .mcp-columns {
      display: block;
    }
    .mcp-columns :global(.pane-resizer) {
      display: none;
    }
    .mcp-list,
    .mcp-detail {
      max-height: none;
    }
    .mcp-detail {
      border-left: 0;
      border-top: 1px solid var(--border, #ccc);
    }
  }
</style>
