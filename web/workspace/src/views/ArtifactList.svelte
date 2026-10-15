<!-- SPDX-License-Identifier: MIT -->
<!-- The project's artifacts (research artifacts, RA-37; docs/specs/pdf-viewer.md, PV-4):
     results first, published or declared, then every other file the runs' calls wrote,
     of one session or run, or with a path that has some text, if the user names it. -->
<script lang="ts">
  import { type ArtifactFilter, byRevision, NO_FILTER } from '../lib/artifactFiles';
  import ArtifactTable from './ArtifactTable.svelte';

  let { projectId }: { projectId: string } = $props();
  let session = $state(''),
    run = $state(''),
    contains = $state('');
  // Applied when the user leaves a field, not on every key.
  let filter = $state<ArtifactFilter>({ ...NO_FILTER });
  function apply() {
    const next = { session: session.trim(), run: run.trim(), contains };
    if (JSON.stringify(next) !== JSON.stringify(filter)) filter = next;
  }
</script>

<h2>Artifacts</h2>
<p class="filters">
  <label>Path contains <input bind:value={contains} onchange={apply} size="20" /></label>
  <label>Session <input bind:value={session} onchange={apply} size="16" /></label>
  <label>Run <input bind:value={run} onchange={apply} size="16" /></label>
</p>
{#if byRevision(filter)}
  <p class="muted">
    Listed: the files with a revision of this session or run. Each row shows its latest revision,
    which someone else may have made.
  </p>
{/if}
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
