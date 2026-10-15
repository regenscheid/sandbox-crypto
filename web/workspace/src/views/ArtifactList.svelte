<!-- SPDX-License-Identifier: MIT -->
<!-- The project's artifacts (research artifacts, RA-37; docs/specs/pdf-viewer.md, PV-4):
     results first, published or declared, then every other file the runs' calls wrote,
     of one session or run if the user names it. -->
<script lang="ts">
  import type { ArtifactFilter } from '../lib/artifactFiles';
  import ArtifactTable from './ArtifactTable.svelte';

  let { projectId }: { projectId: string } = $props();
  let session = $state(''),
    run = $state('');
  // Applied when the user leaves a field, not on every key.
  let filter = $state<ArtifactFilter>({ session: '', run: '' });
  function apply() {
    if (filter.session !== session.trim() || filter.run !== run.trim()) {
      filter = { session: session.trim(), run: run.trim() };
    }
  }
</script>

<h2>Artifacts</h2>
<p class="filters">
  <label>Session <input bind:value={session} onchange={apply} size="16" /></label>
  <label>Run <input bind:value={run} onchange={apply} size="16" /></label>
</p>
<h3>Results</h3>
<ArtifactTable
  {projectId}
  group="results"
  {filter}
  empty="No results yet. An agent publishes a file with publish; a delegated run declares its results."
/>
<details>
  <summary>All files</summary>
  <ArtifactTable
    {projectId}
    group="files"
    {filter}
    empty="No other recorded files. Every file a call writes is recorded, unless the project records results only or an ignore pattern leaves it out."
  />
</details>

<style>
  .filters label {
    margin-right: 1em;
  }
</style>
