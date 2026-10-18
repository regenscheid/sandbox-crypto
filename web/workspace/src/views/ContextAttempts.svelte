<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { ContextAttempt } from '../lib/api-types';
  import { runHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  let {
    project,
    session,
    run,
    close,
  }: {
    project: string;
    session: string;
    run: string;
    close: () => void;
  } = $props();
  let items = $state<ContextAttempt[]>([]);
  let after = $state<string | null>('');
  let loading = $state(false);
  let error = $state<unknown>(null);
  async function load() {
    if (loading || after === null) return;
    loading = true;
    error = null;
    try {
      const page = await api.contextAttempts(project, session, run, after);
      items = page.attempts;
      after = page.next_after;
    } catch (caught) {
      error = caught;
    } finally {
      loading = false;
    }
  }
</script>

<details
  ontoggle={(event) => {
    if (event.currentTarget.open && !items.length) void load();
  }}
>
  <summary>Continuation attempts of this agent</summary>
  <p class="muted">
    These attempts share a recorded work identity. Children and siblings have separate histories. A
    legacy attempt without that identity appears alone.
  </p>
  {#if error}<ErrorMessage {error} />{/if}
  <ul>
    {#each items as item (item.run)}
      <li>
        {#if item.run === run}<strong>Selected: {item.run}</strong>
        {:else if item.saved}<a href={runHref(project, session, item.run)} onclick={close}
            >{item.run}</a
          >{:else}<span>{item.run} (no saved run record; use history across continuations)</span
          >{/if}
        — {item.outcome ?? 'no terminal outcome recorded'}
        {#if item.epoch !== null}
          · epoch {item.epoch}{/if}
        {#if item.predecessor}<div class="muted">Continues {item.predecessor}</div>{/if}
      </li>
    {/each}
  </ul>
  {#if after !== null}<button type="button" disabled={loading} onclick={() => void load()}
      >{loading ? 'Loading attempts…' : items.length ? 'Next attempts' : 'Load attempts'}</button
    >{/if}
</details>
