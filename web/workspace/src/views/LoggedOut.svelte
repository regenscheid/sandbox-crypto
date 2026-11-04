<!-- SPDX-License-Identifier: MIT -->
<!-- The page once the user logged out (D104). Behind Authentik, the token its outpost sends
     opens no workspace session again until it expires, so logging in again goes through
     Authentik's own sign-out, which the server's answer named. -->
<script lang="ts">
  let { signOut = null }: { signOut?: string | null } = $props();
  const link = $derived(signOut?.startsWith('https://') ? signOut : null);
</script>

{#if link}
  <p>
    Logged out. Your Authentik login no longer opens a session here: to log in again,
    <a href={link} rel="noreferrer">sign out of Authentik</a>, then sign in again.
  </p>
{:else}
  <p>
    Logged out. To log in again, open the login URL that sandbox-science-workspace printed or, if
    you came through Authentik, sign out of Authentik and sign in again.
  </p>
{/if}
