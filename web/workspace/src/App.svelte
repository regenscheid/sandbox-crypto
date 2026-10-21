<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { guardPendingUploads } from './lib/uploadDrafts.svelte';
  import { focusPanel } from './lib/focusPanel';
  import { api } from './lib/api';
  import WorkspaceSidebar from './views/WorkspaceSidebar.svelte';
  import Icon from './views/Icon.svelte';
  import { parseRoute } from './lib/router';
  import DocumentView from './views/DocumentView.svelte';
  import HistoryView from './views/HistoryView.svelte';
  import PdfView from './views/PdfView.svelte';
  import FindingView from './views/FindingView.svelte';
  import FrontierList from './views/FrontierList.svelte';
  import FrontierView from './views/FrontierView.svelte';
  import LedgerBoard from './views/LedgerBoard.svelte';
  import MemoryPage from './views/MemoryPage.svelte';
  import SkillsPage from './views/SkillsPage.svelte';
  import McpServices from './views/McpServices.svelte';
  import MessageView from './views/MessageView.svelte';
  import IssueList from './views/IssueList.svelte';
  import ProjectList from './views/ProjectList.svelte';
  import ProjectView from './views/ProjectView.svelte';
  import RunEventsView from './views/RunEventsView.svelte';
  import RunFilesView from './views/RunFilesView.svelte';
  import RunTreeView from './views/RunTreeView.svelte';
  import SessionView from './views/SessionView.svelte';
  import ProjectMath from './views/ProjectMath.svelte';

  let hash = $state(window.location.hash);
  const route = $derived(parseRoute(hash));
  let loggedOut = $state(false);
  let navigationOpen = $state(window.innerWidth > 760);
  let narrow = $state(window.innerWidth <= 760);
  const inSession = $derived(route.view === 'session' || route.view === 'review');
  function closeDrawer() {
    if (window.innerWidth <= 760) navigationOpen = false;
  }

  $effect(() => {
    const follow = () => {
      hash = window.location.hash;
    };
    window.addEventListener('hashchange', follow);
    return () => window.removeEventListener('hashchange', follow);
  });

  async function logout() {
    await api.logout();
    loggedOut = true;
  }
</script>

<svelte:window
  onbeforeunload={guardPendingUploads}
  onresize={() => {
    narrow = window.innerWidth <= 760;
  }}
  onkeydown={(event) => {
    if (event.key === 'Escape' && window.innerWidth <= 760) navigationOpen = false;
  }}
/>
<a
  class="skip-link"
  href="#workspace-content"
  onclick={(event) => {
    event.preventDefault();
    document.getElementById('workspace-content')?.focus();
  }}>Skip to content</a
>
<div
  class="workspace-shell"
  class:nav-open={navigationOpen && !loggedOut}
  class:nav-closed={!navigationOpen && !loggedOut}
>
  {#if !loggedOut}
    <div
      class="sidebar-slot"
      id="workspace-navigation"
      hidden={!navigationOpen}
      use:focusPanel={{
        active: navigationOpen && narrow,
        close: () => {
          navigationOpen = false;
        },
      }}
      role={narrow ? 'dialog' : undefined}
      aria-modal={narrow && navigationOpen ? true : undefined}
      aria-label="Workspace navigation"
      tabindex="-1"
    >
      {#if narrow}<button
          class="drawer-close"
          onclick={() => {
            navigationOpen = false;
          }}>Close navigation ×</button
        >{/if}
      <WorkspaceSidebar
        {route}
        close={closeDrawer}
        hide={narrow
          ? null
          : () => {
              navigationOpen = false;
            }}
        {logout}
      />
    </div>
    {#if navigationOpen}<button
        class="navigation-scrim"
        aria-label="Close navigation"
        onclick={() => {
          navigationOpen = false;
        }}
      ></button>{/if}
  {/if}
  <div class="workspace-body" inert={navigationOpen && narrow}>
    {#if !navigationOpen && !loggedOut}<button
        class="nav-toggle"
        type="button"
        aria-label="Show navigation"
        aria-expanded="false"
        aria-controls="workspace-navigation"
        onclick={() => {
          navigationOpen = true;
        }}><Icon name="sidebar" /></button
      >{/if}
    <main
      id="workspace-content"
      class:session-main={inSession}
      class:ledger-main={route.view === 'ledger' || route.view === 'entry'}
      tabindex="-1"
    >
      <ProjectMath projectId={'project' in route ? route.project : null}>
        {#if loggedOut}
          <p>
            Logged out. To log in again, open the login URL that sandbox-science-workspace printed.
          </p>
        {:else if route.view === 'projects'}
          <ProjectList />
        {:else if route.view === 'project'}
          {#key route.project}
            <ProjectView projectId={route.project} />
          {/key}
        {:else if route.view === 'session'}
          {#key `${route.project}/${route.session}`}
            <SessionView projectId={route.project} sessionId={route.session} />
          {/key}
        {:else if route.view === 'files'}
          {#key `${route.project}/${route.session}/${route.run}`}
            <RunFilesView projectId={route.project} sessionId={route.session} run={route.run} />
          {/key}
        {:else if route.view === 'tree'}
          {#key `${route.project}/${route.session}`}
            <RunTreeView projectId={route.project} sessionId={route.session} />
          {/key}
        {:else if route.view === 'run'}
          {#key `${route.project}/${route.session}/${route.run}`}
            <RunEventsView projectId={route.project} sessionId={route.session} run={route.run} />
          {/key}
        {:else if route.view === 'review'}
          {#key `${route.project}/${route.session}/${route.review}`}
            <SessionView
              projectId={route.project}
              sessionId={route.session}
              reviewId={route.review}
            />
          {/key}
        {:else if route.view === 'finding'}
          {#key `${route.project}/${route.session}/${route.finding}`}
            <FindingView
              projectId={route.project}
              sessionId={route.session}
              findingId={route.finding}
            />
          {/key}
        {:else if route.view === 'document'}
          {#key `${route.project}/${route.document}/${route.revision}`}
            <DocumentView
              projectId={route.project}
              documentId={route.document}
              revision={route.revision}
              block={route.block}
            />
          {/key}
        {:else if route.view === 'memory'}
          {#key route.project}
            <MemoryPage projectId={route.project} />
          {/key}
        {:else if route.view === 'skills'}
          {#key route.project}
            <SkillsPage projectId={route.project} />
          {/key}
        {:else if route.view === 'mcp'}
          {#key route.project}
            <McpServices projectId={route.project} />
          {/key}
        {:else if route.view === 'message'}
          {#key `${route.project}/${route.message}`}
            <MessageView projectId={route.project} messageId={route.message} />
          {/key}
        {:else if route.view === 'ledger' || route.view === 'entry'}
          {#key route.project}
            <LedgerBoard
              projectId={route.project}
              entryName={route.view === 'entry' ? route.entry : null}
              revision={route.view === 'entry' ? route.revision : null}
            />
          {/key}
        {:else if route.view === 'issues'}
          {#key route.project}
            <IssueList projectId={route.project} />
          {/key}
        {:else if route.view === 'categories'}
          {#key route.project}
            <FrontierList projectId={route.project} />
          {/key}
        {:else if route.view === 'frontier'}
          {#key `${route.project}/${route.category}`}
            <FrontierView projectId={route.project} category={route.category} />
          {/key}
        {:else if route.view === 'pdf'}
          {#key `${route.project}/${route.artifact}/${route.revision}/${route.sha256}/${route.page}`}
            <PdfView
              projectId={route.project}
              artifactId={route.artifact}
              revision={route.revision}
              sha256={route.sha256}
              page={route.page}
            />
          {/key}
        {:else if route.view === 'history'}
          {#key `${route.project}/${route.document}`}
            <HistoryView projectId={route.project} documentId={route.document} />
          {/key}
        {:else}
          <p>There is nothing at this address. <a href="#/">See the projects.</a></p>
        {/if}
      </ProjectMath>
    </main>
  </div>
</div>
