<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api } from './lib/api';
  import { parseRoute } from './lib/router';
  import DocumentView from './views/DocumentView.svelte';
  import HistoryView from './views/HistoryView.svelte';
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
    {:else if route.view === 'document'}
      {#key `${route.project}/${route.document}/${route.revision}`}
        <DocumentView
          projectId={route.project}
          documentId={route.document}
          revision={route.revision}
          block={route.block}
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
