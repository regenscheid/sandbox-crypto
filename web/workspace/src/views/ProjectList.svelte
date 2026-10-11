<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api } from '../lib/api';
  import { formatTime, shortRevision } from '../lib/format';
  import { projectHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';

  const loading = api.projects();
</script>

<h1>Projects</h1>

{#await loading}
  <p class="muted">Loading…</p>
{:then listed}
  {#if listed.projects.length === 0}
    <p>
      There are no projects yet. Create one with
      <code>sandbox-science-harness project create</code>.
    </p>
  {:else}
    <table>
      <thead>
        <tr>
          <th>Project</th>
          <th>Base profile</th>
          <th>Policy revision</th>
          <th>Sessions</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody>
        {#each listed.projects as project (project.project_id)}
          <tr>
            {#if project.error}
              <td><code>{project.project_id}</code></td>
              <td colspan="4" class="problem">{project.error}</td>
            {:else}
              <td><a href={projectHref(project.project_id)}>{project.project_id}</a></td>
              <td>{project.profile}</td>
              <td><code title={project.policy}>{shortRevision(project.policy)}</code></td>
              <td>{project.sessions}</td>
              <td>{formatTime(project.updated_at)}</td>
            {/if}
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
{:catch error}
  <ErrorMessage {error} />
{/await}
