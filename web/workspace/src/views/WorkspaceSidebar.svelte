<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api, type Project } from '../lib/api';
  import {
    projectHref,
    sessionHref,
    ledgerHref,
    memoryHref,
    mcpHref,
    skillsHref,
    type Route,
  } from '../lib/router';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import PollStatus from './PollStatus.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  let { route, close }: { route: Route; close: () => void } = $props();
  const projects = api.projects();
  const projectId = $derived('project' in route ? route.project : null);
  const sessionId = $derived('session' in route ? route.session : null);
  let current = $state<PollState<Project>>(initialPoll());
  $effect(() => {
    const project = projectId;
    current = initialPoll();
    if (!project) return;
    const poller = new Poller(
      (signal) => api.project(project, signal),
      (value) => {
        current = value;
      },
      { intervalMs: 5000 },
    );
    return poller.watch();
  });
  let search = $state('');
</script>

<nav class="workspace-sidebar" aria-label="Workspace navigation">
  <a class="workspace-brand" href="#/" onclick={close}
    ><span class="brand-mark">S</span>Sandbox Science</a
  >
  <div class="sidebar-section">
    <span class="eyebrow">Workspace</span>
    <a href="#/" aria-current={route.view === 'projects' ? 'page' : undefined} onclick={close}
      >All projects</a
    >
    {#await projects then listed}
      <label class="sr-only" for="project-choice">Choose project</label>
      <select
        id="project-choice"
        value={projectId ?? ''}
        onchange={(event) => {
          if (event.currentTarget.value) {
            location.hash = projectHref(event.currentTarget.value);
            close();
          }
        }}
      >
        <option value="" disabled>Choose a project</option>
        {#each listed.projects as project (project.project_id)}
          {#if !('error' in project)}<option value={project.project_id}>{project.project_id}</option
            >{/if}
        {/each}
      </select>
    {:catch error}<ErrorMessage {error} />{/await}
  </div>
  {#if projectId}
    <div class="sidebar-section">
      <span class="eyebrow">Research</span>
      <a
        href={projectHref(projectId)}
        onclick={close}
        aria-current={route.view === 'project' ? 'page' : undefined}>Project overview</a
      >
      <a
        href={ledgerHref(projectId)}
        onclick={close}
        aria-current={route.view === 'ledger' || route.view === 'entry' ? 'page' : undefined}
        >Research ledger</a
      >
      <a
        href={memoryHref(projectId)}
        onclick={close}
        aria-current={route.view === 'memory' ? 'page' : undefined}>Project memory</a
      >
      <a
        href={skillsHref(projectId)}
        onclick={close}
        aria-current={route.view === 'skills' ? 'page' : undefined}>Skills</a
      >
      <a
        href={mcpHref(projectId)}
        onclick={close}
        aria-current={route.view === 'mcp' ? 'page' : undefined}>MCP services</a
      >
    </div>
    <div class="sidebar-section sessions">
      <span class="eyebrow">Sessions</span>
      <input
        type="search"
        aria-label="Search sessions"
        placeholder="Search sessions…"
        bind:value={search}
      />
      <PollStatus phase={current.phase} hasData={current.data !== null} what="Sessions" />
      {#if current.data}{@const project = current.data}
        {#if project}
          {#each [...project.sessions]
            .reverse()
            .filter((s) => s.session_id
                .toLowerCase()
                .includes(search.toLowerCase())) as session (session.session_id)}
            <a
              href={sessionHref(projectId, session.session_id)}
              onclick={close}
              aria-current={sessionId === session.session_id ? 'page' : undefined}
            >
              <span class="session-dot" class:closed={session.closed_at !== null}></span>
              <span class="session-name">{session.session_id}</span>
              {#if session.closed_at}<span class="muted">Closed</span>{/if}
            </a>
          {:else}<p class="muted">{search ? 'No matching sessions.' : 'No sessions yet.'}</p>{/each}
        {/if}
      {/if}
    </div>
  {/if}
  <div class="sidebar-footer"><span class="session-dot"></span>Research workspace</div>
</nav>
