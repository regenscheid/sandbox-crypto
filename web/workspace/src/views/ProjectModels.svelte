<!-- SPDX-License-Identifier: MIT -->
<!-- Project model aliases and efforts. A save affects the next root tree; admitted trees
     keep their pinned profiles. -->
<script lang="ts">
  import type { AgentModelSelection, ModelSettings } from '../lib/api-types';
  import { api } from '../lib/api';
  import { guardedWrite } from '../lib/mutation';
  import { idle, type Submitting } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId }: { projectId: string } = $props();
  type AgentModelRole = 'main' | 'delegated' | 'reviewer';
  let settings = $state<ModelSettings | null>(null);
  let expected = $state(0);
  let head = $state(0);
  let roles = $state<Partial<Record<AgentModelRole, AgentModelSelection>>>({});
  let status = $state('');
  const saving = $state<Submitting>(idle());
  const order: AgentModelRole[] = ['main', 'delegated', 'reviewer'];
  const labels: Record<AgentModelRole, string> = {
    main: 'Orchestrator',
    delegated: 'Delegated agents',
    reviewer: 'Consistency reviewer',
  };

  function assign(current: ModelSettings) {
    settings = current;
    expected = head = current.revision;
    roles = structuredClone(current.roles);
  }

  const loading = $derived(api.modelSettings(projectId).then(assign));

  function option(alias: string) {
    return settings?.available.find((candidate) => candidate.alias === alias);
  }

  function changedAlias(role: AgentModelRole) {
    const selected = roles[role];
    if (selected && option(selected.alias)?.effort_supported === false) {
      selected.reasoning_effort = null;
    }
  }

  function useDefaults() {
    if (settings) roles = structuredClone(settings.defaults);
  }

  async function save() {
    if (!settings || saving.busy) return;
    saving.busy = true;
    saving.error = null;
    status = '';
    try {
      const body = { expected, roles: $state.snapshot(roles) };
      const changed = await guardedWrite('project-models', projectId, body, (sent) =>
        api.changeModelSettings(projectId, sent),
      );
      assign(changed);
      status = 'Model settings saved. New research trees will use them.';
    } catch (error) {
      saving.error = error;
      try {
        const current = await api.modelSettings(projectId);
        head = current.revision;
        settings = current;
      } catch {
        // Keep the original error and the user's draft.
      }
    } finally {
      saving.busy = false;
    }
  }
</script>

<details>
  <summary>Agent models and reasoning effort</summary>
  <p class="muted">
    These choices apply when the next research tree starts. Active agents, their children and an
    ordinary Resume keep the profiles they already pinned.
  </p>
  {#await loading}
    <p role="status">Loading agent model settings…</p>
  {:then}
    {#if settings}
      <div class="role-grid">
        {#each order.filter((role) => roles[role] !== undefined) as role (role)}
          <fieldset>
            <legend>{labels[role]}</legend>
            <label>
              Model
              <select
                bind:value={roles[role]!.alias}
                disabled={saving.busy}
                onchange={() => changedAlias(role)}
              >
                {#each settings.available as candidate (candidate.alias)}
                  <option value={candidate.alias}>{candidate.alias}</option>
                {/each}
              </select>
            </label>
            <label>
              Reasoning effort
              <select
                value={roles[role]!.reasoning_effort ?? ''}
                disabled={saving.busy || option(roles[role]!.alias)?.effort_supported === false}
                onchange={(event) => {
                  roles[role]!.reasoning_effort = event.currentTarget.value || null;
                }}
              >
                <option value="">Provider default</option>
                {#each settings.known_efforts as effort (effort)}
                  <option value={effort}>{effort}</option>
                {/each}
              </select>
            </label>
            {#if option(roles[role]!.alias)?.effort_supported === false}
              <p class="muted">This model's Messages adapter does not accept an effort setting.</p>
            {/if}
          </fieldset>
        {/each}
      </div>
      <p class="muted">Based on revision {expected}. Current revision {head}.</p>
      {#if expected !== head}
        <p role="alert">The saved settings changed. Your draft is still here; review it first.</p>
        <button type="button" disabled={saving.busy} onclick={() => (expected = head)}
          >Use current revision</button
        >
      {/if}
      <div class="actions">
        <button type="button" disabled={saving.busy} onclick={useDefaults}
          >Use deployment defaults</button
        >
        <button type="button" disabled={saving.busy} onclick={save}>Save agent models</button>
      </div>
      {#if status}<p role="status">{status}</p>{/if}
      {#if saving.error}<ErrorMessage error={saving.error} />{/if}
    {/if}
  {:catch error}
    <ErrorMessage {error} />
  {/await}
</details>

<style>
  .role-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
    gap: 0.8rem;
  }
  fieldset,
  label {
    display: grid;
    gap: 0.4rem;
  }
  select {
    width: 100%;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem;
  }
</style>
