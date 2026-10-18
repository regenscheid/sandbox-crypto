<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import type { ProjectSession } from '../lib/api-types';
  import { reviews } from '../lib/reviews';
  import { sessionHref } from '../lib/router';
  let { projectId, sessions }: { projectId: string; sessions: ProjectSession[] } = $props();
  const sample = $derived(
    [...sessions].sort((a, b) => b.opened_at.localeCompare(a.opened_at)).slice(0, 8),
  );
  const observations = $derived(
    Promise.all(
      sample.map(async (session) => {
        try {
          const page = await reviews.list(projectId, session.session_id);
          return {
            session: session.session_id,
            pending: page.reviews.filter((review) => ['queued', 'running'].includes(review.state))
              .length,
            unreviewed: page.unreviewed.length,
            more: page.next_after !== null,
            unavailable: false,
          };
        } catch {
          return {
            session: session.session_id,
            pending: 0,
            unreviewed: 0,
            more: false,
            unavailable: true,
          };
        }
      }),
    ),
  );
</script>

<section aria-label="Review inbox">
  <h2>Review inbox</h2>
  <p class="muted">
    Latest review page in up to eight recently opened sessions. Incomplete or unavailable
    observations do not establish that a session has no pending review.
  </p>
  {#await observations}<p>Loading review observations…</p>{:then rows}<ul>
      {#each rows as row (row.session)}<li>
          <a href={sessionHref(projectId, row.session)}>{row.session}</a>: {row.unavailable
            ? 'Review observation unavailable'
            : `${row.pending} queued or running reviews; ${row.unreviewed} unreviewed observations${row.more ? '; earlier reviews not loaded' : ''}`}
        </li>{:else}<li>No sessions yet.</li>{/each}
    </ul>{/await}
</section>
