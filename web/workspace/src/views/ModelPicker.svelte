<!-- SPDX-License-Identifier: MIT -->
<!-- The research agent's model and reasoning effort, beside the message box. A choice is
     saved to the project's model settings for the main agent (ProjectModels.svelte has
     all roles): it applies when the next research tree starts, and agents already running
     keep the profiles they pinned. -->
<script lang="ts">
  import type { ModelSettings } from '../lib/api-types';
  import { api } from '../lib/api';
  import { guardedWrite } from '../lib/mutation';
  import { projectHref } from '../lib/router';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';
  import Icon from './Icon.svelte';

  let { projectId }: { projectId: string } = $props();
  let settings = $state<ModelSettings | null>(null);
  let failure = $state<unknown>(null);
  let open = $state(false);
  let saving = $state(false);
  let root: HTMLElement;
  const main = $derived(settings?.roles['main'] ?? null);
  const option = $derived(settings?.available.find((item) => item.alias === main?.alias));
  const effortLabel = (effort: string | null) =>
    effort ? effort.charAt(0).toUpperCase() + effort.slice(1) : 'Default';

  $effect(() => {
    const project = projectId;
    api.modelSettings(project).then(
      (current) => {
        // A deployment without model settings answers no roles: show no picker.
        settings = current?.roles && current.available ? current : null;
      },
      (error) => {
        failure = error;
      },
    );
  });

  async function choose(alias: string, effort: string | null) {
    if (!settings || saving) return;
    const supported = settings.available.find((item) => item.alias === alias)?.effort_supported;
    const roles = $state.snapshot(settings.roles);
    roles['main'] = { alias, reasoning_effort: supported === false ? null : effort };
    saving = true;
    failure = null;
    try {
      const body = { expected: settings.revision, roles };
      settings = await guardedWrite('project-models', projectId, body, (sent) =>
        api.changeModelSettings(projectId, sent),
      );
    } catch (error) {
      failure = error;
      try {
        settings = await api.modelSettings(projectId);
      } catch {
        // Keep the error shown.
      }
    } finally {
      saving = false;
    }
  }
</script>

<svelte:window
  onpointerdown={(event) => {
    if (open && !root.contains(event.target as Node)) open = false;
  }}
  onkeydown={(event) => {
    if (open && event.key === 'Escape') open = false;
  }}
/>
<div class="model-picker" bind:this={root}>
  {#if main}
    <button
      type="button"
      class="model-trigger"
      aria-haspopup="dialog"
      aria-expanded={open}
      title="The research agent's model"
      onclick={() => (open = !open)}
    >
      <strong>{visible(main.alias)}</strong>
      {#if option?.effort_supported !== false}<span class="muted"
          >{effortLabel(main.reasoning_effort)}</span
        >{/if}
      <Icon name="down" size={14} />
    </button>
  {/if}
  {#if open && settings && main}
    <div class="model-popover" role="dialog" aria-label="Research agent model">
      <div>
        <span class="eyebrow">Model</span>
        <div class="model-options" role="radiogroup" aria-label="Model">
          {#each settings.available as candidate (candidate.alias)}
            <button
              type="button"
              class="model-option"
              role="radio"
              aria-checked={candidate.alias === main.alias}
              disabled={saving}
              onclick={() => choose(candidate.alias, main.reasoning_effort)}
              ><span>{visible(candidate.alias)}</span>{#if candidate.alias === main.alias}<Icon
                  name="check"
                  size={14}
                />{/if}</button
            >
          {/each}
        </div>
      </div>
      {#if option?.effort_supported !== false}
        <div>
          <span class="eyebrow">Reasoning effort</span>
          <div class="effort-group" role="radiogroup" aria-label="Reasoning effort">
            {#each [null, ...settings.known_efforts] as effort (effort ?? '')}
              <button
                type="button"
                role="radio"
                aria-checked={main.reasoning_effort === effort}
                disabled={saving}
                onclick={() => choose(main.alias, effort)}>{effortLabel(effort)}</button
              >
            {/each}
          </div>
        </div>
      {/if}
      <div class="composer-note">
        Applies when the next research tree starts; agents already running keep theirs. Subagents
        and reviewers use the models in <a href={projectHref(projectId)}>project settings</a>.
      </div>
      {#if failure}<ErrorMessage error={failure} />{/if}
    </div>
  {/if}
</div>
