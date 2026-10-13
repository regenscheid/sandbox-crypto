<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { onMount } from 'svelte';
  import { authorLabel, onPlanLogout, type PlanObservation } from '../lib/plans';
  import { PlanEditSession, textFields, sameRef, type EditorView } from '../lib/planEdits';
  let { projectId, sessionId, observation, observed, refresh }:
    {projectId: string; sessionId: string; observation: PlanObservation; observed: boolean; refresh: () => void} = $props();
  let editor = $state<PlanEditSession | null>(null);
  let view = $state<EditorView | null>(null);
  const draft = $derived(view?.draft);
  const fields = $derived(textFields(draft?.body ?? observation.plan!.version.body));
  const uncertain = $derived(draft?.pending?.state === 'unknown');
  const disabled = $derived(!view?.eligible || view.busy || uncertain);
  const current = $derived(view?.current.plan ?? observation.plan!);
  const changed = $derived(draft ? !sameRef(draft.base, current.head.ref) : false);
  onMount(() => {
    const session = new PlanEditSession({projectId, sessionId}, observation, observed, value => {view = value;}, undefined, refresh);
    editor = session;
    const off = onPlanLogout(() => session.logout());
    return () => {off(); session.stop();};
  });
  $effect(() => {editor?.observe(observation, observed);});
</script>

<details class="plan-editor">
  <summary>Edit plan text</summary>
  <p>Change wording in the existing plan. Saving does not approve the plan, start work, or change progress.</p>
  <p class="muted">Current version {current.head.ref.version} by {authorLabel(current.version.author)}.
    {#if draft}Your draft uses version {draft.base.version}.{/if}</p>
  {#if observation.session_closed_at !== null || ['completed', 'abandoned'].includes(current.head.status)}
    <p>This recorded plan is read-only.</p>
  {:else if !observed}<p>Editing waits for a current plan observation.</p>{/if}
  {#if view?.warning}<p role="alert">Draft storage is unavailable. Keep this tab open and copy your text before leaving; a save may be held until its retry can be retained.</p>{/if}
  {#if view?.blocked}<p role="alert">A saved draft could not be read safely. It has been left untouched; editing is held.</p>{/if}
  {#if view?.signedOut}<p role="status">Editing stopped for sign-out. Reload after signing in; an earlier save may still be unresolved.</p>{/if}
  {#if view?.message}<p role="status">{view.message}</p>{/if}
  {#if changed && !uncertain}<p>Your draft is based on an older version. Review the current wording above before keeping your draft or loading current text.</p>{/if}
  {#if uncertain}<p role="alert">The earlier save may already have happened. Keep its exact request for retry; signing in again does not prove it is the same operation.</p>{/if}
  <form onsubmit={event => {event.preventDefault(); void editor?.save();}}>
    <fieldset disabled={disabled}>
      {#each fields as field (field.key)}
        <label>{field.label}<textarea rows={field.max === 128 ? 2 : 3} maxlength={field.max * 2} value={field.value}
          oninput={event => editor?.edit(field.key, event.currentTarget.value)}></textarea></label>
      {/each}
      <label>Reason for the change<textarea rows="2" maxlength="8192" value={draft?.reason ?? 'User plan edit'}
        oninput={event => editor?.reason(event.currentTarget.value)}></textarea></label>
    </fieldset>
    <div class="actions">
      <button type="submit" disabled={!view?.eligible || view.busy}>{view?.busy ? 'Saving…' : uncertain ? 'Retry exact save' : 'Save plan text'}</button>
      {#if changed && !uncertain}
        <button type="button" disabled={disabled} onclick={() => editor?.adopt()}>Keep draft; use current version</button>
      {/if}
      {#if !uncertain}<button type="button" disabled={disabled || !!draft?.retained} onclick={() => editor?.loadCurrent()}>Load current; retain draft</button>{/if}
    </div>
  </form>
  {#if uncertain}
    <button type="button" disabled={!view?.eligible || view.busy || !view.canContinue} onclick={() => editor?.continueCurrent()}>Continue from current; keep earlier save unresolved</button>
    {#if !view?.canContinue}<p class="muted">A fresh current observation is required. Only one earlier unresolved save can be retained.</p>{/if}
  {/if}
  {#if draft?.retained}
    <details><summary>Retained draft from version {draft.retained.base.version}</summary>
      {#each textFields(draft.retained.body) as field (field.key)}<label>{field.label}<textarea readonly rows="2" value={field.value}></textarea></label>{/each}
      <label>Earlier reason<textarea readonly rows="2" value={draft.retained.reason}></textarea></label>
    </details>
  {/if}
  {#if draft?.unresolved}
    <details><summary>Earlier save still unresolved</summary>
      <p>This request may have committed. Continuing did not prove failure or merge its text.</p>
      <p class="muted">Based on version {draft.unresolved.base.version}. Save reference: <code>{draft.unresolved.operation_id}</code></p>
      {#each textFields(draft.unresolved.body) as field (field.key)}<label>{field.label}<textarea readonly rows="2" value={field.value}></textarea></label>{/each}
      <label>Earlier reason<textarea readonly rows="2" value={draft.unresolved.reason}></textarea></label>
    </details>
  {/if}
</details>

<style>
  .plan-editor { margin-block: 1rem; padding: .75rem; border: 1px solid var(--border); border-radius: .3rem; }
  form, fieldset { border: 0; padding: 0; margin: 0; }
  label { display: block; margin-block: .75rem; }
  textarea { display: block; width: 100%; box-sizing: border-box; margin-top: .25rem; resize: vertical; }
  .actions { display: flex; flex-wrap: wrap; gap: .5rem; margin-block: .75rem; }
</style>
