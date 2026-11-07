<!-- SPDX-License-Identifier: MIT -->
<!-- The page once its session ended: the user logged out, or the server stopped knowing
     the session (2 hours without a request, 12 hours after it opened, a logout elsewhere
     or a restart). Behind Authentik, the token its outpost sends opens no workspace
     session again after a logout until it expires, so logging in again goes through
     Authentik's own sign-out, which the server's answer named (D104). The login link
     works once: the server makes a new one at each login, which `login-url` prints. -->
<script lang="ts">
  let {
    reason = 'logout',
    login = null,
    signOut = null,
  }: {
    reason?: 'logout' | 'ended';
    login?: 'token' | 'forward' | null;
    signOut?: string | null;
  } = $props();
  const link = $derived(signOut?.startsWith('https://') ? signOut : null);
</script>

{#if reason === 'logout' && link}
  <p>
    Logged out. Your Authentik login no longer opens a session here: to log in again,
    <a href={link} rel="noreferrer">sign out of Authentik</a>, then sign in again.
  </p>
{:else}
  <p>
    {#if reason === 'logout'}
      Logged out.
    {:else}
      The session ended. A session ends 2 hours after its last request, 12 hours after it opened, at
      a logout, or when the server restarts.
    {/if}
    {#if login === 'forward' && reason === 'logout'}
      To log in again, sign out of Authentik, then sign in again.
    {:else if login === 'forward'}
      To log in again, reload the page to sign in through Authentik.
    {:else if login === 'token'}
      To log in again, open the current login link: each link works once, and the server makes a new
      one at each login. <code>sandbox-science-workspace login-url</code> prints it; without Authentik
      it is the last one the server printed.
    {:else}
      To log in again, open the current login link (each link works once:
      <code>sandbox-science-workspace login-url</code> prints the current one; without Authentik it is
      the last one the server printed), or sign in again through Authentik.
    {/if}
  </p>
{/if}
