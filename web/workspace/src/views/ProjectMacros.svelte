<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import { macroDrafts } from '../lib/drafts';
  import { adoptCurrent, macroChange, savedDraft } from '../lib/macroSettings';
  import { guardedWrite } from '../lib/mutation';
  import { idle, submit } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  let { projectId }: { projectId: string } = $props();
  let draft = $state(untrack(() => macroDrafts.load(projectId).draft) ?? { expected: 0, text: '' });
  let head = $state(0),
    persisted = $state(true),
    status = $state('');
  const saving = $state(idle());
  async function refresh() {
    const current = await api.projectMacros(projectId);
    head = current.revision;
    draft = adoptCurrent(draft, current);
  }
  const loading = refresh();
  $effect(() => {
    if (draft.text) persisted = macroDrafts.save(projectId, $state.snapshot(draft));
  });
  async function save() {
    status = '';
    await submit(
      saving,
      async () => {
        const result = await guardedWrite('macros', projectId, macroChange(draft), (sent) =>
          api.changeMacros(projectId, sent),
        );
        draft = savedDraft(result);
        head = result.revision;
        macroDrafts.clear(projectId);
        status = `Saved project macro revision ${head}. Math checks refresh at the next active run; reload other open document views to adopt this context.`;
        window.dispatchEvent(new Event('project-macros-changed'));
      },
      refresh, // the current revision, beside the retained draft
    );
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (draft.text && !persisted) event.preventDefault();
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
        bind:value={draft.text}></textarea></label
    >
    <p>Based on project macro revision {draft.expected}. Current revision {head}.</p>
    {#if draft.expected !== head}<button onclick={() => (draft.expected = head)}
        >Use current macro revision</button
      >
      <p>Your definitions are retained; review them before saving.</p>{/if}
    <button disabled={saving.busy} onclick={save}>Save project macros</button>
    {#if status}<p role="status">{status}</p>{/if}
  {:catch caught}<ErrorMessage error={caught} />{/await}
  {#if saving.error}<ErrorMessage error={saving.error} />{/if}
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
