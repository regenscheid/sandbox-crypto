<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { ApiError } from '../lib/api';

  let { error }: { error: unknown } = $props();
</script>

{#if error instanceof ApiError && error.loggedOut}
  <p class="problem">
    Not logged in. Reload the page to sign in through Authentik, or open the current login link,
    which <code>sandbox-science-workspace login-url</code> prints; each link works once.
    {#if error.message !== 'signed out'}<span class="muted">({error.message})</span>{/if}
  </p>
{:else if error instanceof ApiError}
  <p class="problem">{error.message} <span class="muted">({error.kind})</span></p>
{:else}
  <p class="problem">The workspace server could not be reached: {String(error)}</p>
{/if}
