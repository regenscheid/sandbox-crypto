<!-- SPDX-License-Identifier: MIT -->
<!-- One pinned image (docs/specs/figures.md, FG-12, FG-13): its bytes from the preview
     origin, for a capability the workspace mints when it is shown. An image the server
     will not give, or the preview origin refuses, shows as its path with the reason: never
     a blank. -->
<script lang="ts">
  import { api, ApiError } from '../lib/api';
  import { imageSource } from '../lib/figures';

  let {
    projectId,
    artifact,
    revision,
    sha256,
    alt,
  }: { projectId: string; artifact: string; revision: number; sha256: string; alt: string } =
    $props();
  let source = $state<string | null>(null);
  let problem = $state<string | null>(null);

  async function load() {
    try {
      const link = await api.figureLink(projectId, artifact, revision, sha256);
      source = imageSource(link.url);
      if (source === null) problem = 'the server gave no usable address';
    } catch (caught) {
      problem = caught instanceof ApiError ? caught.message : 'the server could not be reached';
    }
  }
  void load();
</script>

{#if problem !== null}
  <span class="problem">Not shown: <bdi>{alt}</bdi> ({problem}).</span>
{:else if source !== null}
  <img
    src={source}
    {alt}
    referrerpolicy="no-referrer"
    loading="lazy"
    onerror={() => (problem = 'the preview origin did not show it as an image')}
  />
{:else}
  <span class="muted">Loading…</span>
{/if}

<style>
  img {
    display: block;
    max-width: 100%;
    height: auto;
  }
</style>
