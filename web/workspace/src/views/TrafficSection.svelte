<!-- SPDX-License-Identifier: MIT -->
<!-- "Inspected traffic", folded: opened, it reads the MCP calls the policy middleware
     inspected for a run, or for one tool row's call in its run, and shows them
     (InspectedTraffic). It reads once when opened, and again when opened later. -->
<script lang="ts">
  import type { TrafficCall } from '../lib/api-types';
  import type { StepCall } from '../lib/conversation';
  import { callTraffic, readTraffic } from '../lib/mcpTraffic';
  import InspectedTraffic from './InspectedTraffic.svelte';

  let {
    projectId,
    sessionId,
    run,
    call = null,
  }: {
    projectId: string;
    sessionId: string;
    run: string;
    /** A chat row's call: only its tool's calls are read, and matched by arguments. */
    call?: Pick<StepCall, 'name' | 'arguments'> | null;
  } = $props();
  let view = $state<
    | { phase: 'closed' | 'loading' }
    | { phase: 'shown'; calls: TrafficCall[]; note: string | null }
    | { phase: 'failed'; message: string }
  >({ phase: 'closed' });

  async function load() {
    view = { phase: 'loading' };
    try {
      const found = await readTraffic(projectId, {
        session: sessionId,
        run,
        ...(call ? { tool: call.name } : {}),
      });
      if (found.unavailable !== null && found.calls.length === 0) {
        view = {
          phase: 'failed',
          message: `Inspected traffic is unavailable: ${found.unavailable}.`,
        };
        return;
      }
      let calls = found.calls;
      const notes: string[] = [];
      if (call) {
        const matched = callTraffic(found.calls, call);
        calls = matched.matched;
        if (!matched.exact && calls.length)
          notes.push(
            "Every inspected call of this tool in the run: the recorded arguments do not match this row's exactly (cut or redacted).",
          );
      }
      if (found.more) notes.push('Later calls are not shown: the read stopped at its page limit.');
      view = { phase: 'shown', calls, note: notes.join(' ') || null };
    } catch (error) {
      view = { phase: 'failed', message: `Inspected traffic could not be read: ${String(error)}` };
    }
  }

  function toggled(event: Event) {
    if ((event.currentTarget as HTMLDetailsElement).open) void load();
  }
</script>

<details class="inspected" ontoggle={toggled}>
  <summary>Inspected traffic</summary>
  {#if view.phase === 'loading'}
    <p class="muted" role="status">Reading the inspected traffic…</p>
  {:else if view.phase === 'failed'}
    <p class="problem" role="status">{view.message}</p>
  {:else if view.phase === 'shown'}
    {#if view.calls.length === 0}
      <p class="muted">No MCP traffic was inspected for {call ? 'this call' : 'this run'}.</p>
    {:else}
      <InspectedTraffic calls={view.calls} note={view.note} />
    {/if}
  {/if}
</details>
