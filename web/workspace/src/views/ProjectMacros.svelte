<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import { loadDraft, saveDraft, clearDraft } from '../lib/drafts';
  import { documentRequest, documentAnswered } from '../lib/documentRequests';
  import ErrorMessage from './ErrorMessage.svelte';
  let { projectId }: { projectId: string } = $props();
  const key = 'project-macros';
  const draft = untrack(() => loadDraft(projectId, key));
  let text = $state(draft?.text ?? ''),
    expected = $state(draft?.base ?? 0),
    head = $state(0);
  let busy = $state(false),
    error = $state<unknown>(null),
    persisted = $state(true),
    status = $state('');
  async function refresh() {
    const current = await api.projectMacros(projectId);
    head = current.revision;
    if (!text) {
      text = JSON.stringify(current.macros, null, 2);
      expected = head;
    }
  }
  const loading = refresh();
  $effect(() => {
    if (text)
      persisted = saveDraft(projectId, key, {
        base: expected,
        text,
        reason: '',
        retained: '',
        block: null,
      });
  });
  async function save() {
    busy = true;
    error = null;
    status = '';
    try {
      const payload = { expected, macros: JSON.parse(text) as unknown };
      const request = await documentRequest(projectId, key, payload);
      const result = await api.changeMacros(projectId, { ...payload, operation_id: request.id });
      documentAnswered(request);
      expected = head = result.revision;
      text = JSON.stringify(result.macros, null, 2);
      clearDraft(projectId, key);
      status = `Saved project macro revision ${head}. Math checks refresh at the next active run; reload other open document views to adopt this context.`;
      window.dispatchEvent(new Event('project-macros-changed'));
    } catch (caught) {
      error = caught;
      try {
        await refresh();
      } catch {
        /* Keep the existing draft. */
      }
    } finally {
      busy = false;
    }
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (text && !persisted) event.preventDefault();
  }}
/>
<details>
  <summary>Project math macros</summary>
  <p>
    Document front matter overrides these definitions. USER settings apply to project text and
    inherited document/comment contexts.
  </p>
  {#await loading}<p>Loading macros…</p>{:then}
    <label
      >Macro definitions as JSON<textarea
        aria-label="Project macro definitions"
        rows="7"
        maxlength="16384"
        bind:value={text}></textarea></label
    >
    <p>Based on project macro revision {expected}. Current revision {head}.</p>
    {#if expected !== head}<button onclick={() => (expected = head)}
        >Use current macro revision</button
      >
      <p>Your definitions are retained; review them before saving.</p>{/if}
    <button disabled={busy} onclick={save}>Save project macros</button>
    {#if status}<p role="status">{status}</p>{/if}
  {:catch caught}<ErrorMessage error={caught} />{/await}
  {#if error}<ErrorMessage {error} />{/if}
  {#if !persisted}<p role="alert">
      Browser storage is unavailable. Keep this draft open or copy it before leaving.
    </p>{/if}
</details>

<style>
  textarea {
    width: 100%;
    box-sizing: border-box;
    font-family: monospace;
  }
</style>
