<!-- SPDX-License-Identifier: MIT -->
<!-- The project's artifact list, filtered to the files one run of a session recorded, as
     a delegation's files in the run tree link to it (research artifacts, RA-36, RA-37). -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { projectHref, sessionHref, treeHref } from '../lib/router';
  import { visible } from '../lib/text';
  import ArtifactList from './ArtifactList.svelte';

  let { projectId, sessionId, run }: { projectId: string; sessionId: string; run: string } =
    $props();
  // The route keys this view by the project, session and run: they never change here.
  const initial = untrack(() => ({ session: sessionId, run, contains: '' }));
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
  <a href={sessionHref(projectId, sessionId)}>{sessionId}</a> /
  <a href={treeHref(projectId, sessionId)}>Run tree</a> /
</nav>
<h1>Files of run {visible(run)}</h1>
<ArtifactList {projectId} {initial} />
