<!-- SPDX-License-Identifier: MIT -->
<!-- An entry's versions, newest first, 50 at a time (docs/specs/project-memory.md, PM-1,
     PM-4): what each changed, its exact text (ApprovalText), who proposed it and why, and
     that the user accepted or made it; a move says the text did not change. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { MemoryVersion } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { versionText } from '../lib/memory';
  import { emptyList, PagedList } from '../lib/pagedList';
  import { visible } from '../lib/text';
  import ApprovalText from './ApprovalText.svelte';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId, entry }: { projectId: string; entry: string } = $props();
  const versions = $state(emptyList<MemoryVersion, number>());
  const list = new PagedList(versions, async (before) => {
    const found = await api.memoryHistory(projectId, entry, before);
    return { items: found.versions, next: found.next_before };
  });
  void list.reload();
</script>

<ol class="history">
  {#each versions.items as version (version.version)}
    <li>
      <p>
        {versionText(version)}, {formatTime(version.at)}{#if version.reason}; the reason given: <bdi
            >{visible(version.reason)}</bdi
          >{/if}
      </p>
      {#if version.text !== null && version.change !== 'moved'}<ApprovalText
          text={version.text}
          label="Its text then"
        />{/if}
    </li>
  {/each}
</ol>
{#if versions.loading}<p class="muted">Loading…</p>{/if}
{#if versions.error}<ErrorMessage error={versions.error} />{/if}
{#if versions.next !== null}
  <button type="button" disabled={versions.loading} onclick={() => list.more()}
    >Earlier versions</button
  >
{/if}

<style>
  .history {
    overflow-wrap: anywhere;
  }
</style>
