<!-- SPDX-License-Identifier: MIT -->
<!-- The review findings anchored to an entry, each with its own status (docs/specs/
     ledger.md, LG-25), leading to the finding in its session. A finding's status is its
     own: a change to the entry resolves none. -->
<script lang="ts">
  import type { EntryFinding } from '../lib/api-types';
  import { findingLink, findingText } from '../lib/ledgerEvidence';

  let { projectId, findings }: { projectId: string; findings: EntryFinding[] } = $props();
</script>

{#if findings.length}
  <h2>Review findings</h2>
  <ul class="findings">
    {#each findings as finding (finding.finding_id)}
      <li>
        <a href={findingLink(projectId, finding)}>{finding.finding_id}</a>
        <span class="muted">({findingText(finding)}; session {finding.session})</span>
      </li>
    {/each}
  </ul>
{/if}
