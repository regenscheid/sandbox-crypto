<!-- SPDX-License-Identifier: MIT -->
<!-- The project's settings on one page, each in its own section: the agents' models, what
     the model calls cost, the files executions record, the math macros, the environments,
     and the policy the project runs under. -->
<script lang="ts">
  import { api } from '../lib/api';
  import { formatCost, formatTime } from '../lib/format';
  import { costByRole, costSource } from '../lib/modelCost';
  import EnvironmentPanel from './EnvironmentPanel.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  import ProjectFiles from './ProjectFiles.svelte';
  import ProjectMacros from './ProjectMacros.svelte';
  import ProjectModels from './ProjectModels.svelte';

  let { projectId }: { projectId: string } = $props();
  const loading = $derived(api.project(projectId));
  const sections = [
    ['settings-models', 'Models'],
    ['settings-spending', 'Spending'],
    ['settings-files', 'Files'],
    ['settings-math', 'Math macros'],
    ['settings-environments', 'Environments'],
    ['settings-policy', 'Policy'],
  ] as const;
  function jump(event: MouseEvent, id: string) {
    event.preventDefault();
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
</script>

<div class="settings">
  <nav class="settings-nav" aria-label="Settings sections">
    {#each sections as [id, label] (id)}
      <a href={`#${id}`} onclick={(event) => jump(event, id)}>{label}</a>
    {/each}
  </nav>
  <div class="settings-body">
    <div class="page-head">
      <div>
        <h1>Project settings</h1>
        <p class="muted">{projectId}</p>
      </div>
    </div>
    {#await loading}
      <p class="muted">Loading…</p>
    {:then project}
      <section id="settings-models" aria-labelledby="settings-models-h">
        <h2 id="settings-models-h">Models</h2>
        <p class="muted">
          Which model each role uses. A change applies when the next research tree starts; agents
          already running keep the model they started with.
        </p>
        <ProjectModels {projectId} open />
      </section>
      <section id="settings-spending" aria-labelledby="settings-spending-h">
        <h2 id="settings-spending-h">Spending</h2>
        {#if project.model_cost.total !== null}
          <div class="tiles">
            <div class="tile">
              <span class="muted">Last 24 hours</span><strong
                >{formatCost(project.model_cost.last_day ?? 0)}</strong
              >
            </div>
            <div class="tile">
              <span class="muted">This project</span><strong
                >{formatCost(project.model_cost.total)}</strong
              >
            </div>
          </div>
          <p class="muted">{costSource(project.model_cost)}</p>
          {#if costByRole(project.model_cost).length}
            <ul>
              {#each costByRole(project.model_cost) as line (line)}
                <li>{line}</li>
              {/each}
            </ul>
          {/if}
        {:else}
          <p class="muted">No model cost recorded yet.</p>
        {/if}
      </section>
      <section id="settings-files" aria-labelledby="settings-files-h">
        <h2 id="settings-files-h">Files</h2>
        <p class="muted">Which files the agents' executions record as artifacts.</p>
        <ProjectFiles {projectId} open />
      </section>
      <section id="settings-math" aria-labelledby="settings-math-h">
        <h2 id="settings-math-h">Math macros</h2>
        <ProjectMacros {projectId} />
      </section>
      <section id="settings-environments">
        <EnvironmentPanel {projectId} {project} />
      </section>
      <section id="settings-policy" aria-labelledby="settings-policy-h">
        <h2 id="settings-policy-h">Policy</h2>
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
      </section>
    {:catch error}
      <ErrorMessage {error} />
    {/await}
  </div>
</div>

<style>
  .settings {
    display: flex;
    flex-wrap: wrap;
    gap: 32px;
  }
  .settings-nav {
    flex: 1 1 160px;
    max-width: 190px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding-top: 64px;
    position: sticky;
    top: 0;
    align-self: flex-start;
  }
  .settings-nav a {
    display: flex;
    align-items: center;
    min-height: 34px;
    padding: 0 10px;
    border-radius: 8px;
    color: var(--body-text);
  }
  .settings-nav a:hover {
    background: var(--subtle);
    text-decoration: none;
  }
  .settings-body {
    flex: 999 1 520px;
    min-width: 0;
    max-width: 860px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  section {
    padding: 16px 0 24px;
    border-bottom: 1px solid var(--subtle);
    scroll-margin-top: 16px;
  }
  section h2 {
    font-size: 15px;
    font-weight: 600;
    margin: 0 0 4px;
  }
  section > p.muted {
    margin: 0 0 12px;
  }
  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 10px;
    margin: 8px 0;
  }
  .tile {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 12px 14px;
  }
  .tile strong {
    font-size: 20px;
    font-weight: 600;
  }
  @media (max-width: 760px) {
    .settings-nav {
      display: none;
    }
  }
</style>
