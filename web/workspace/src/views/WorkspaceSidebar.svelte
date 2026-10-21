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
    documentsHref,
    artifactsHref,
    settingsHref,
    type Route,
  } from '../lib/router';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import BrandMark from './BrandMark.svelte';
  import Icon, { type IconName } from './Icon.svelte';
  import PollStatus from './PollStatus.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  let {
    route,
    close,
    hide = null,
    logout = null,
  }: {
    route: Route;
    close: () => void;
    /** Hide the navigation, where the page offers it. */
    hide?: (() => void) | null;
    logout?: (() => void) | null;
  } = $props();
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
  const sessions = $derived(
    [...(current.data?.sessions ?? [])]
      .reverse()
      .filter((s) => s.session_id.toLowerCase().includes(search.toLowerCase())),
  );
  const links = $derived<{ label: string; href: string; icon: IconName; current: boolean }[]>(
    projectId
      ? [
          {
            label: 'Overview',
            href: projectHref(projectId),
            icon: 'overview',
            current: route.view === 'project',
          },
          {
            label: 'Documents',
            href: documentsHref(projectId),
            icon: 'document',
            current:
              route.view === 'documents' || route.view === 'document' || route.view === 'history',
          },
          {
            label: 'Artifacts',
            href: artifactsHref(projectId),
            icon: 'artifact',
            current: route.view === 'artifacts' || route.view === 'pdf',
          },
          {
            label: 'Ledger',
            href: ledgerHref(projectId),
            icon: 'ledger',
            current: route.view === 'ledger' || route.view === 'entry',
          },
          {
            label: 'Memory',
            href: memoryHref(projectId),
            icon: 'memory',
            current: route.view === 'memory',
          },
          {
            label: 'Skills',
            href: skillsHref(projectId),
            icon: 'skill',
            current: route.view === 'skills',
          },
          {
            label: 'Connectors',
            href: mcpHref(projectId),
            icon: 'connector',
            current: route.view === 'mcp',
          },
        ]
      : [],
  );
</script>

<nav class="workspace-sidebar" aria-label="Workspace navigation">
  <div class="sidebar-top">
    <a class="workspace-brand" href="#/" onclick={close}
      ><BrandMark /><span>Cryptography Atlas</span></a
    >
    {#if hide}<button
        class="nav-toggle"
        type="button"
        aria-label="Hide navigation"
        aria-controls="workspace-navigation"
        aria-expanded="true"
        onclick={hide}><Icon name="sidebar" /></button
      >{/if}
  </div>
  {#await projects then listed}
    <label class="project-choice"
      ><span>Project</span><select
        id="project-choice"
        aria-label="Choose project"
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
      </select></label
    >
  {:catch error}<ErrorMessage {error} />{/await}
  {#if projectId}
    <div class="sidebar-section sessions">
      <span class="eyebrow">Sessions</span>
      {#if (current.data?.sessions.length ?? 0) > 6}<input
          type="search"
          aria-label="Search sessions"
          placeholder="Search sessions…"
          bind:value={search}
        />{/if}
      <PollStatus phase={current.phase} hasData={current.data !== null} what="Sessions" />
      {#if current.data}
        {#each sessions as session (session.session_id)}
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
    </div>
    <div class="sidebar-section">
      <span class="eyebrow">Project</span>
      {#each links as link (link.label)}
        <a href={link.href} onclick={close} aria-current={link.current ? 'page' : undefined}
          ><Icon name={link.icon} /><span class="session-name">{link.label}</span></a
        >
      {/each}
    </div>
  {/if}
  <div class="sidebar-footer">
    {#if projectId}<a
        href={settingsHref(projectId)}
        onclick={close}
        aria-current={route.view === 'settings' ? 'page' : undefined}
        ><Icon name="settings" />Settings</a
      >{/if}
    <a href="#/" onclick={close} aria-current={route.view === 'projects' ? 'page' : undefined}
      ><Icon name="projects" />All projects</a
    >
    {#if logout}<button type="button" onclick={logout}><Icon name="logout" />Log out</button>{/if}
  </div>
</nav>
