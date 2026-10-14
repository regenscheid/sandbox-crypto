<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { setContext, untrack, type Snippet } from 'svelte';
  import { api } from '../lib/api';
  import { macroSet, NO_MACROS, type MacroSet } from '../lib/macros';
  let { projectId, children }: { projectId: string | null; children: Snippet } = $props();
  let macros = $state<MacroSet>(NO_MACROS),
    error = $state(false),
    loading = $state(false);
  let loadedFor = $state<string | null>(null);
  setContext('project-macros', () => macros);
  let refresh = $state(0);
  $effect(() => {
    const changed = () => {
      refresh += 1;
    };
    window.addEventListener('project-macros-changed', changed);
    return () => window.removeEventListener('project-macros-changed', changed);
  });
  $effect(() => {
    void refresh;
    const project = projectId;
    error = false;
    if (project === null) {
      macros = NO_MACROS;
      loadedFor = null;
      loading = false;
      return;
    }
    const first = untrack(() => loadedFor) !== project;
    if (first) macros = NO_MACROS;
    let current = true;
    if (first) loading = true;
    api
      .projectMacros(project)
      .then((found) => {
        if (current) {
          macros = macroSet(found.macros, `${project}:${found.revision}:${found.sha256}`);
          loadedFor = project;
        }
      })
      .catch(() => {
        if (current) {
          error = true;
          loadedFor = project;
        }
      })
      .finally(() => {
        if (current) loading = false;
      });
    return () => {
      current = false;
    };
  });
</script>

{#if loading}<p role="status">Loading project math settings…</p>{:else}
  {#if error}<p role="status">
      Project macro context could not refresh; unscoped text uses its last loaded diagnostic
      context.
    </p>{/if}
  {@render children()}
{/if}
