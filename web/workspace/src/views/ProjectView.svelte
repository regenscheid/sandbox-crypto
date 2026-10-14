<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api } from '../lib/api';
  import { formatTime } from '../lib/format';
  import { documentHref, sessionHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import ProjectMacros from './ProjectMacros.svelte';
  import EnvironmentPanel from './EnvironmentPanel.svelte';

  let { projectId }: { projectId: string } = $props();
  const loading = $derived(api.project(projectId));
  const documents = $derived(api.documents(projectId));
</script>

<nav class="trail"><a href="#/">Projects</a> /</nav>
<h1>{projectId}</h1>

{#await loading}
  <p class="muted">Loading…</p>
{:then project}
  <dl class="facts">
    <dt>Base profile</dt>
    <dd>{project.profile}</dd>
    <dt>Policy revision</dt>
    <dd><code>{project.policy}</code></dd>
    <dt>Default environment</dt>
    <dd><code>{project.default_environment_id}</code></dd>
    <dt>Created</dt>
    <dd>{formatTime(project.created_at)}</dd>
    <dt>Updated</dt>
    <dd>{formatTime(project.updated_at)}</dd>
  </dl>
  <ProjectMacros {projectId} />

  <h2>Sessions</h2>
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
        {#each project.sessions as session (session.session_id)}
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

  <h2>Documents</h2>
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
          {#each listed as found (found.document_id)}
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

  <EnvironmentPanel {projectId} {project} />
{:catch error}
  <ErrorMessage {error} />
{/await}
