<!-- SPDX-License-Identifier: MIT -->
<!-- Reads the gate for an entry and the decisions that affect it (`GET …/ledger/{entry}/
     gate`), and reads it again after the user's change to the entry, which can change it:
     the panel shown stays until the new read arrives. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { LedgerGate } from '../lib/api-types';
  import { Detail, noDetail } from '../lib/detail';
  import ErrorMessage from './ErrorMessage.svelte';
  import LedgerGatePanel from './LedgerGatePanel.svelte';

  let {
    projectId,
    entryId,
    shown,
    changes = 0,
  }: {
    projectId: string;
    entryId: string;
    /** The revision the entry view shows. */
    shown: number;
    /** Counts the user's changes to the entry: after one, the gate is read again. */
    changes?: number;
  } = $props();
  const read = $state(noDetail<string, LedgerGate>());
  // The entry view is keyed by the entry it shows: the ID never changes here.
  const detail = new Detail(read, (entry) => api.ledgerGate(projectId, entry));
  void detail.show(untrack(() => entryId));
  let seen = untrack(() => changes);
  $effect(() => {
    if (changes === seen) return;
    seen = changes;
    void detail.reload();
  });
</script>

{#if read.value}
  <LedgerGatePanel {projectId} gate={read.value.gate} {shown} />
{:else if read.loading}
  <h2>Gate and decisions</h2>
  <p class="muted">Loading…</p>
{/if}
{#if read.error}<ErrorMessage error={read.error} />{/if}
