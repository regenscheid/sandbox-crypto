<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { browserState } from '../lib/browserState';
  import { api } from '../lib/api';
  import { formatTime } from '../lib/format';
  import {
    artifactsHref,
    documentHref,
    documentsHref,
    settingsHref,
    ledgerHref,
    mcpHref,
    memoryHref,
    monitorHref,
    usageHref,
    sessionHref,
    skillsHref,
  } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import ProjectReviewInbox from './ProjectReviewInbox.svelte';

  let { projectId }: { projectId: string } = $props();
  const scope = untrack(() => `overview:${projectId}`);
  const kept = untrack(() => browserState.load(scope).draft ?? {});
  let search = $state(kept.search ?? '');
  let sessionFilter = $state(kept.state ?? 'all');
  $effect(() => {
    browserState.save(scope, { search, state: sessionFilter });
  });
  const loading = $derived(api.project(projectId));
  const documents = $derived(api.documents(projectId));
  const memory = $derived(api.memory(projectId));
</script>

<nav class="trail"><a href="#/">Projects</a> /</nav>
<h1>{projectId}</h1>

{#await loading}
  <p class="muted">Loading…</p>
{:then project}
  <p class="muted">Research overview · updated {formatTime(project.updated_at)}</p>
  <nav class="overview-shortcuts" aria-label="Project shortcuts">
    <a href={documentsHref(projectId)}>Documents</a><a href={artifactsHref(projectId)}>Artifacts</a
    ><a href={ledgerHref(projectId)}>Ledger</a><a href={memoryHref(projectId)}
      >Memory and proposal inbox</a
    ><a href={skillsHref(projectId)}>Skills</a><a href={mcpHref(projectId)}>Connectors</a><a
      href={monitorHref(projectId)}>Monitor</a
    ><a href={usageHref(projectId)}>Usage</a><a href={settingsHref(projectId)}>Settings</a>
  </nav>
  {#await memory then found}<p>
      {found.memory.waiting} memory proposals await a decision.
      <a href={memoryHref(projectId)}>Review proposals</a>
    </p>{:catch error}<ErrorMessage {error} />{/await}
  <h2>Sessions</h2>
  <label>Search sessions <input type="search" bind:value={search} /></label>
  <label
    >Session state <select bind:value={sessionFilter}
      ><option value="all">All</option><option value="open">Open</option><option value="closed"
        >Closed</option
      ></select
    ></label
  >
  {#if project.sessions.length === 0}
    <p>No sessions yet. A session starts with <code>sandbox-science-harness run</code>.</p>
  {:else}
    <table>
      <thead>
        <tr>
          <th>Session</th>
          <th>Environment</th>
          <th>Runs</th>
          <th>Opened</th>
          <th>State</th>
        </tr>
      </thead>
      <tbody>
        {#each project.sessions.filter((item) => item.session_id
              .toLocaleLowerCase()
              .includes(search.toLocaleLowerCase()) && (sessionFilter === 'all' || (sessionFilter === 'closed') === Boolean(item.closed_at))) as session (session.session_id)}
          <tr>
            <td><a href={sessionHref(projectId, session.session_id)}>{session.session_id}</a></td>
            <td>
              <code>{session.environment_id}</code>
              <span class="muted">{session.dedicated ? 'its own' : "the project's"}</span>
            </td>
            <td>{session.runs}</td>
            <td>{formatTime(session.opened_at)}</td>
            <td>{session.closed_at ? `closed ${formatTime(session.closed_at)}` : 'open'}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <ProjectReviewInbox {projectId} sessions={project.sessions} />
  <h2>Recent documents</h2>
  <p>
    <a href={documentsHref(projectId)}>All documents</a> ·
    <a href={artifactsHref(projectId)}>All artifacts</a>
  </p>
  {#await documents}
    <p class="muted">Loading…</p>
  {:then listed}
    {#if listed.length === 0}
      <p>
        No documents yet. Agents create them with the document tool; the user imports them with <code
          >sandbox-science-harness document import</code
        >.
      </p>
    {:else}
      <table>
        <thead>
          <tr>
            <th>Document</th>
            <th>Title</th>
            <th>Where</th>
            <th>Revision</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          {#each [...listed]
            .sort((a, b) => b.created_at.localeCompare(a.created_at))
            .slice(0, 5) as found (found.document_id)}
            <tr>
              <td><a href={documentHref(projectId, found.document_id)}>{found.name}</a></td>
              <td>{found.title}</td>
              <td>
                {found.scope === 'project'
                  ? 'the project'
                  : `session ${found.session_id}'s workbook`}
              </td>
              <td>{found.head}</td>
              <td>{formatTime(found.created_at)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  {:catch error}
    <ErrorMessage {error} />
  {/await}
{:catch error}
  <ErrorMessage {error} />
{/await}

<style>
  .overview-shortcuts {
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
    padding: 1rem 0;
  }
  label {
    display: inline-grid;
    gap: 0.3rem;
    margin: 0.5rem;
  }
</style>
