<!-- SPDX-License-Identifier: MIT -->
<!-- Viewing reads safe cached proxy metadata; only an explicit check lists upstream tools. -->
<script lang="ts">
  import { untrack, onMount } from 'svelte';
  import { browserState } from '../lib/browserState';
  import { post } from '../lib/http';
  import { api } from '../lib/api';
  import type { ApprovalSettings, ConnectorSettings, McpInventory } from '../lib/api-types';
  import { guardedWrite } from '../lib/mutation';
  import { formatTime } from '../lib/format';
  import { projectHref } from '../lib/router';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';
  import PaneResizer from './PaneResizer.svelte';

  let { projectId }: { projectId: string } = $props();
  let shown = $state<McpInventory | null>(null);
  let error = $state<unknown>(null);
  const scope = untrack(() => `mcp:${projectId}`);
  const kept = untrack(() => browserState.load(scope).draft ?? {});
  let selected = $state<string | null>(kept.selected || null);
  let search = $state(kept.search ?? '');
  let filter = $state(kept.filter ?? 'all');
  let tab = $state(kept.tab ?? 'overview');
  let width = $state(
    Math.min(Number(kept.width) || 460, Math.max(320, Math.floor(window.innerWidth / 2))),
  );
  let toolSearch = $state(kept.toolSearch ?? '');
  let selectedTool = $state(kept.selectedTool ?? '');
  let checking = $state(false);
  let now = $state(Date.now());
  onMount(() => {
    const timer = setInterval(() => {
      now = Date.now();
    }, 30_000);
    return () => clearInterval(timer);
  });
  $effect(() => {
    browserState.save(scope, {
      selected: selected ?? '',
      search,
      filter,
      tab,
      width: String(width),
      toolSearch,
      selectedTool,
    });
  });
  const stale = (at: string | null | undefined) => !at || now - Date.parse(at) > 300_000;
  async function check() {
    if (!current || checking) return;
    checking = true;
    try {
      await post(
        `/projects/${encodeURIComponent(projectId)}/mcp-services/${encodeURIComponent(current.name)}/check`,
        {},
        { deadlineMs: 20_000 },
      );
      await load();
    } catch (caught) {
      error = caught;
    } finally {
      checking = false;
    }
  }
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
    services.find((service) => service.name === selected) ?? filtered[0] ?? null,
  );

  // Which services the user turned off for the project (narrowing only).
  let settings = $state<ConnectorSettings | null>(null);
  let toggling = $state(false);
  let toggleError = $state<unknown>(null);
  const isOff = (name: string) => settings?.disabled.includes(name) ?? false;
  async function toggle(name: string) {
    if (settings === null || toggling) return;
    const disabled = isOff(name)
      ? settings.disabled.filter((item) => item !== name)
      : [...settings.disabled, name];
    toggling = true;
    toggleError = null;
    try {
      settings = await guardedWrite(
        'connectors',
        projectId,
        { expected: settings.revision, disabled },
        (change) => api.changeConnectorSettings(projectId, change),
      );
    } catch (caught) {
      toggleError = caught;
      try {
        settings = await api.connectorSettings(projectId);
      } catch {
        // Keep the first error shown.
      }
    } finally {
      toggling = false;
    }
  }

  // The tool calls the user approved for the whole project, which they may take back.
  let remembered = $state<ApprovalSettings | null>(null);
  async function forget(server: string, tool: string | null) {
    if (remembered === null || toggling) return;
    const grants = remembered.grants.filter(
      (grant) => !(grant.server === server && (grant.tool ?? null) === tool),
    );
    toggling = true;
    toggleError = null;
    try {
      remembered = await guardedWrite(
        'approvals',
        projectId,
        { expected: remembered.revision, grants },
        (change) => api.changeApprovalSettings(projectId, change),
      );
    } catch (caught) {
      toggleError = caught;
      try {
        remembered = await api.approvalSettings(projectId);
      } catch {
        // Keep the first error shown.
      }
    } finally {
      toggling = false;
    }
  }

  async function load() {
    try {
      api.approvalSettings(projectId).then(
        (found) => {
          // A server without these settings answers something else: show none.
          remembered = Array.isArray(found?.grants) ? found : null;
        },
        () => {
          remembered = null;
        },
      );
      api.connectorSettings(projectId).then(
        (found) => {
          settings = Array.isArray(found?.disabled) ? found : null;
        },
        (caught) => {
          toggleError = caught;
        },
      );
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
<h1>Connectors</h1>
<p class="muted">
  Configured services and observations from a bounded sample of recorded runs. The most recent
  result shown for a service may not be its latest run. Viewing never invokes a tool. Use Check
  availability for a bounded metadata listing through the proxy.
</p>
{#if error}<ErrorMessage {error} />
  <p role="status">Inventory refresh failed; any retained observation is previous data.</p>
  <button type="button" onclick={() => void load()}>Retry inventory</button>
{/if}
{#if shown === null}<p class="muted">Loading…</p>
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
            ><strong>{visible(service.name)}</strong>{#if isOff(service.name)}<span class="pill"
                >Off</span
              >{/if}<span class="state"
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
        {#if settings}
          <div class="connector-switch">
            <span
              ><strong
                >{isOff(current.name) ? 'Off for this project' : 'On for this project'}</strong
              ><small class="muted"
                >{isOff(current.name)
                  ? 'Agents in this project are not offered its tools, and their calls to it are refused.'
                  : "This project's agents can use its tools, as the policy allows."}</small
              ></span
            >
            <button
              type="button"
              class:primary-button={isOff(current.name)}
              disabled={toggling}
              onclick={() => void toggle(current.name)}
              >{isOff(current.name) ? 'Turn on' : 'Turn off'}</button
            >
          </div>
          {#if toggleError}<ErrorMessage error={toggleError} />{/if}
        {/if}
        {#if remembered}
          {@const mine = remembered.grants.filter((grant) => grant.server === current.name)}
          <div class="remembered">
            <strong>Approved for this project</strong>
            {#each mine as grant (grant.tool ?? '*')}
              <div class="remembered-row">
                <span>{grant.tool ? visible(grant.tool) : 'Any tool the policy asks about'}</span
                ><button
                  type="button"
                  disabled={toggling}
                  onclick={() => void forget(grant.server, grant.tool ?? null)}>Remove</button
                >
              </div>
            {:else}
              <small class="muted"
                >None. Calls the policy asks about wait for your answer in the chat.</small
              >
            {/each}
          </div>
        {/if}
        <p>
          Proxy: {current.proxy_status ?? 'unknown'}{#if current.proxy_checked_at}
            · observed {formatTime(current.proxy_checked_at)}{/if}.
        </p>
        <p>
          Availability: {current.availability
            ? `${current.availability.status}${stale(current.availability.checked_at) ? ' · stale' : ''} · checked ${formatTime(current.availability.checked_at)}`
            : 'Unknown · no explicit availability check in this workspace process'}.
        </p>
        <button disabled={checking} onclick={() => void check()}
          >{checking ? 'Checking metadata…' : 'Check availability'}</button
        >
        <p class="muted">
          Checks initialize and list metadata only. A listing does not prove a particular tool call
          will succeed. Observations become stale after five minutes.
        </p>
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
          {#if current.metadata}
            <p class="muted">
              {current.metadata.schema_projection} Listed {current.metadata.listing_at
                ? formatTime(current.metadata.listing_at)
                : 'never'}{#if stale(current.metadata.listing_at)}
                · stale or unknown{/if}. {#if current.metadata.truncated}Partial listing; some
                metadata was omitted.{/if}
            </p>
            {#if current.metadata.listing_policy !== current.metadata.policy.revision}<p
                role="status"
              >
                This listing predates the current policy. Tool decisions below use the current
                policy.
              </p>{/if}
            <label>Search tools <input type="search" bind:value={toolSearch} /></label>
            <ul>
              {#each current.metadata.tools.filter((tool) => `${tool.name} ${tool.description}`
                  .toLocaleLowerCase()
                  .includes(toolSearch.toLocaleLowerCase())) as tool (tool.name)}
                <li>
                  <button
                    aria-expanded={selectedTool === tool.name}
                    onclick={() => (selectedTool = selectedTool === tool.name ? '' : tool.name)}
                    >{tool.name} · {tool.action}</button
                  >
                  {#if selectedTool === tool.name}<p>{visible(tool.description)}</p>
                    <pre>{tool.parameters_json}</pre>{/if}
                </li>
              {:else}<li>
                  No tool metadata in this snapshot. Check availability to request a listing.
                </li>{/each}
            </ul>
          {:else}<p>
              Tool metadata unavailable. No tool names or schemas are inferred from historical
              counts.
            </p>{/if}
        {:else if tab === 'policy'}
          {#if current.metadata}{@const policy = current.metadata.policy}
            {#if current.proxy_status !== 'reachable'}<p role="status">
                Previous policy snapshot; current proxy policy is unknown while the proxy is
                unavailable.
              </p>{/if}
            <dl class="facts">
              <dt>Proxy policy revision</dt>
              <dd><code>{policy.revision}</code></dd>
              <dt>Protocol</dt>
              <dd>{policy.protocol}</dd>
              <dt>Authentication mode</dt>
              <dd>{policy.authentication}</dd>
              <dt>Listing decision</dt>
              <dd>{policy.listing_action}</dd>
              <dt>Default action</dt>
              <dd>{policy.default_action}</dd>
              <dt>Allow tool patterns</dt>
              <dd>{policy.allow.join(', ') || 'None'}</dd>
              <dt>Require approval</dt>
              <dd>{policy.escalate.join(', ') || 'None'}</dd>
              <dt>Deny tool patterns</dt>
              <dd>{policy.deny.join(', ') || 'None'}</dd>
              <dt>Worker access</dt>
              <dd>
                {policy.worker_access ? 'Enabled' : 'Disabled'} · {policy.worker_tools.join(', ')}
              </dd>
              <dt>Payload bounds</dt>
              <dd>
                {policy.argument_bytes.toLocaleString()} argument bytes / {policy.response_bytes.toLocaleString()}
                response bytes
              </dd>
            </dl>
            <p class="muted">
              Per-tool decisions include current policy, hard floor and pool ceiling. Approvals and
              request arguments are checked again when a tool is called.
            </p>
          {:else}<p>
              Applicable proxy policy is unknown because the metadata source is unavailable. Project
              record revision: {shown.policy_revision}.
            </p>{/if}
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
  .remembered {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin: 0 0 12px;
  }
  .remembered-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 6px 10px;
    border: 1px solid var(--border, #ccc);
    border-radius: 8px;
  }
  .connector-switch {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px 16px;
    padding: 12px 14px;
    margin: 4px 0 12px;
    border: 1px solid var(--border, #ccc);
    border-radius: 10px;
  }
  .connector-switch > span {
    flex: 1 1 220px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 24rem;
    overflow: auto;
  }
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
