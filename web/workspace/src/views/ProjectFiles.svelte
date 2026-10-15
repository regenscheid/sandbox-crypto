<!-- SPDX-License-Identifier: MIT -->
<!-- The project's settings for the files its executions write (research artifacts,
     RA-29, RA-39): record every file or results only, and the ignore patterns. -->
<script lang="ts">
  import { api } from '../lib/api';
  import { patternsText, settingsChange } from '../lib/artifactFiles';
  import { guardedWrite } from '../lib/mutation';
  import { idle, submit } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId }: { projectId: string } = $props();
  let expected = $state(0),
    head = $state(0),
    recording = $state<'all' | 'results'>('all'),
    text = $state(''),
    defaults = $state<string[]>([]),
    status = $state('');
  const saving = $state(idle());
  async function refresh(adopt: boolean) {
    const current = await api.fileSettings(projectId);
    head = current.revision;
    defaults = current.defaults ?? [];
    if (adopt) {
      expected = current.revision;
      recording = current.recording;
      text = patternsText(current.ignore);
    }
  }
  const loading = refresh(true);
  async function save() {
    status = '';
    await submit(
      saving,
      async () => {
        const result = await guardedWrite(
          'files',
          projectId,
          settingsChange(expected, recording, text),
          (sent) => api.changeFileSettings(projectId, sent),
        );
        expected = head = result.revision;
        recording = result.recording;
        text = patternsText(result.ignore);
        status = `Saved revision ${head}. Calls that end from now on follow it.`;
      },
      () => refresh(false), // the current revision, beside the edited settings
    );
  }
</script>

<details>
  <summary>Files executions write</summary>
  {#await loading}<p>Loading the file settings…</p>{:then}
    <fieldset>
      <legend>Record</legend>
      <label
        ><input type="radio" bind:group={recording} value="all" /> every file a call writes, except the
        patterns below</label
      >
      <label
        ><input type="radio" bind:group={recording} value="results" /> results only: published files and
        delegated runs' declared results</label
      >
    </fieldset>
    <label
      >Ignore patterns, one a line, as in <code>.gitignore</code><textarea
        aria-label="Ignore patterns"
        rows="8"
        bind:value={text}></textarea></label
    >
    <button type="button" onclick={() => (text = patternsText(defaults))}>Use the defaults</button>
    <p>Based on revision {expected}. Current revision {head}.</p>
    {#if expected !== head}<button type="button" onclick={() => (expected = head)}
        >Use the current revision</button
      >
      <p>Your settings are kept; review them before saving.</p>{/if}
    <button disabled={saving.busy} onclick={save}>Save the file settings</button>
    {#if status}<p role="status">{status}</p>{/if}
  {:catch caught}<ErrorMessage error={caught} />{/await}
  {#if saving.error}<ErrorMessage error={saving.error} />{/if}
</details>

<style>
  textarea {
    width: 100%;
    box-sizing: border-box;
    font-family: monospace;
  }
  fieldset label {
    display: block;
  }
</style>
