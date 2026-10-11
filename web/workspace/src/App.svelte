<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api } from './lib/api';
  import { parseRoute } from './lib/router';
  import ProjectList from './views/ProjectList.svelte';
  import ProjectView from './views/ProjectView.svelte';
  import SessionView from './views/SessionView.svelte';

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
  {:else}
    <p>There is nothing at this address. <a href="#/">See the projects.</a></p>
  {/if}
</main>
