<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api } from './lib/api';
  import { parseRoute } from './lib/router';
  import DocumentView from './views/DocumentView.svelte';
  import HistoryView from './views/HistoryView.svelte';
  import PdfView from './views/PdfView.svelte';
  import FindingView from './views/FindingView.svelte';
  import FrontierList from './views/FrontierList.svelte';
  import FrontierView from './views/FrontierView.svelte';
  import LedgerBoard from './views/LedgerBoard.svelte';
  import LedgerEntryView from './views/LedgerEntryView.svelte';
  import MemoryPage from './views/MemoryPage.svelte';
  import IssueList from './views/IssueList.svelte';
  import ProjectList from './views/ProjectList.svelte';
  import ProjectView from './views/ProjectView.svelte';
  import SessionView from './views/SessionView.svelte';
  import ProjectMath from './views/ProjectMath.svelte';

  let hash = $state(window.location.hash);
  const route = $derived(parseRoute(hash));
  let loggedOut = $state(false);

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

<header class="top">
  <a class="home" href="#/">Sandbox Science workspace</a>
  {#if !loggedOut}
    <button type="button" onclick={logout}>Log out</button>
  {/if}
</header>

<main>
  <ProjectMath projectId={'project' in route ? route.project : null}>
    {#if loggedOut}
      <p>Logged out. To log in again, open the login URL that sandbox-science-workspace printed.</p>
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
    {:else if route.view === 'ledger'}
      {#key route.project}
        <LedgerBoard projectId={route.project} />
      {/key}
    {:else if route.view === 'issues'}
      {#key route.project}
        <IssueList projectId={route.project} />
      {/key}
    {:else if route.view === 'entry'}
      {#key `${route.project}/${route.entry}/${route.revision}`}
        <LedgerEntryView
          projectId={route.project}
          entryName={route.entry}
          revision={route.revision}
        />
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
