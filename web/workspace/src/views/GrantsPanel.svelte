<!-- SPDX-License-Identifier: MIT -->
<!-- The project's grants (D97): what each covers (an effect class on a server, a host for
     research code's reads, or a package source), for a session or the project, when it was
     last used, and Revoke, which ends it at once. Grants are made from the chat's cards. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { GrantListing } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { grantLabel } from '../lib/permissionCards';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId }: { projectId: string } = $props();
  let listing = $state<GrantListing | null>(null);
  let error = $state<unknown>(null);
  let busy = $state(false);

  async function load() {
    try {
      const found = await api.grants(projectId);
      // A server without grants answers something else: show none.
      listing = Array.isArray(found?.grants) ? found : null;
      error = null;
    } catch (caught) {
      error = caught;
    }
  }
  async function revoke(grant: string) {
    if (busy) return;
    busy = true;
    error = null;
    try {
      listing = await api.revokeGrant(projectId, grant);
    } catch (caught) {
      error = caught;
      await load();
    } finally {
      busy = false;
    }
  }
  void load();
</script>

<section class="grants" aria-label="Grants">
  <h2>Grants</h2>
  <p class="muted">
    Standing answers you gave in the chat. Each covers only what the policy asks about, within the
    widest grant it allows; revoking one stops it at once.
  </p>
  {#if error}<ErrorMessage {error} />{/if}
  {#if listing}
    {#each listing.grants as grant (grant.grant_id)}
      <div class="grant-row">
        <span class="what"
          ><strong>{visible(grantLabel(grant))}</strong><small class="muted"
            >{grant.scope === 'project'
              ? 'This project'
              : `Session ${visible(grant.session ?? '')}`}{#if grant.expires_at}
              · until {formatTime(grant.expires_at)}{/if} · {grant.last_used_at
              ? `last used ${formatTime(grant.last_used_at)}`
              : 'not used yet'}</small
          ></span
        ><button type="button" disabled={busy} onclick={() => void revoke(grant.grant_id)}
          >Revoke</button
        >
      </div>
    {:else}
      <p class="muted">None. Requests the policy asks about wait for your answer in the chat.</p>
    {/each}
  {/if}
</section>

<style>
  .grants {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin: 0 0 16px;
  }
  h2 {
    margin: 0;
    font-size: 16px;
  }
  .grants > p {
    margin: 0;
  }
  .grant-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 6px 10px;
    border: 1px solid var(--border, #ccc);
    border-radius: 8px;
  }
  .what {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
