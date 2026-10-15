<!-- SPDX-License-Identifier: MIT -->
<!-- The project's settings for the files its executions write (research artifacts,
     RA-29, RA-39): record every file or results only, and the ignore patterns. -->
<script lang="ts">
  import { api } from '../lib/api';
  import {
    patternProblem,
    patternsText,
    saveSettings,
    type SettingsForm,
  } from '../lib/artifactFiles';
  import { idle } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId }: { projectId: string } = $props();
  const form = $state<SettingsForm>({
    expected: 0,
    head: 0,
    recording: 'all',
    text: '',
    status: '',
  });
  let defaults = $state<string[]>([]);
  const saving = $state(idle());
  const problem = $derived(patternProblem(form.text));
  async function load() {
    const current = await api.fileSettings(projectId);
    form.expected = form.head = current.revision;
    form.recording = current.recording;
    form.text = patternsText(current.ignore);
    defaults = current.defaults ?? [];
  }
  const loading = load();
  function save() {
    return saveSettings(
      form,
      saving,
      projectId,
      (sent) => api.changeFileSettings(projectId, sent),
      () => api.fileSettings(projectId),
    );
  }
</script>

<details>
  <summary>Files executions write</summary>
  {#await loading}<p>Loading the file settings…</p>{:then}
    <fieldset>
      <legend>Record</legend>
      <label
        ><input type="radio" bind:group={form.recording} value="all" /> every file a call writes, except
        the patterns below</label
      >
      <label
        ><input type="radio" bind:group={form.recording} value="results" /> results only: published files
        and delegated runs' declared results</label
      >
    </fieldset>
    <label
      >Ignore patterns, one a line, as in <code>.gitignore</code><textarea
        aria-label="Ignore patterns"
        rows="8"
        bind:value={form.text}></textarea></label
    >
    {#if problem}<p role="alert">{problem}</p>{/if}
    <button type="button" onclick={() => (form.text = patternsText(defaults))}
      >Use the defaults</button
    >
    <p>Based on revision {form.expected}. Current revision {form.head}.</p>
    {#if form.expected !== form.head}<button
        type="button"
        onclick={() => (form.expected = form.head)}>Use the current revision</button
      >
      <p>Your settings are kept; review them before saving.</p>{/if}
    <button disabled={saving.busy || problem !== null} onclick={save}>Save the file settings</button
    >
    {#if form.status}<p role="status">{form.status}</p>{/if}
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
