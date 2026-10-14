<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api, type JobListing } from '../lib/api';
  import { jobElapsed } from '../lib/jobs';
  import ErrorMessage from './ErrorMessage.svelte';
  import JobCard from './JobCard.svelte';

  let { projectId, sessionId }: { projectId: string; sessionId: string } = $props();
  let data = $state<JobListing | null>(null);
  let error = $state<unknown>(null);
  let cancelling = $state<string | null>(null);

  $effect(() => {
    const project = projectId,
      session = sessionId;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    data = null;
    error = null;
    async function refresh(): Promise<void> {
      try {
        const found = await api.jobs(project, session);
        if (live) {
          data = found;
          error = null;
        }
      } catch (found) {
        if (live) error = found;
      } finally {
        if (live) timer = setTimeout(() => void refresh(), 3000);
      }
    }
    void refresh();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  });

  async function cancel(job: string): Promise<void> {
    cancelling = job;
    try {
      await api.cancelJob(projectId, sessionId, job);
      data = await api.jobs(projectId, sessionId);
      error = null;
    } catch (found) {
      error = found;
    } finally {
      cancelling = null;
    }
  }
</script>

<section aria-label="Background jobs">
  <h2>Background jobs</h2>
  {#if error}<ErrorMessage {error} />{/if}
  {#if data}
    {#each data.waits as wait (wait.run_id)}
      <div class="waiting" role="status">
        <strong>{wait.connected ? 'Agent waiting' : 'Waiting harness disconnected'}</strong>
        for {wait.mode === 'all' ? 'all jobs' : 'the first job to end'}:
        {wait.job_ids.join(', ')} · {jobElapsed((Date.now() - Date.parse(wait.started_at)) / 1000)}
        <p class="muted">
          {wait.watcher
            ? `Check-ins by ${wait.watcher}`
            : 'Check-ins are off: no watcher model configured.'}
        </p>
        {#each wait.checkins as check}
          <p>{check.state === 'fine' ? 'Progress' : 'Needs attention'}: {check.note}</p>
        {/each}
      </div>
    {/each}
    {#if data.jobs.length === 0}<p class="muted">No background jobs in this session.</p>{/if}
    {#each data.jobs as job (job.job_id)}
      <JobCard
        {job}
        cancelling={cancelling === job.job_id}
        oncancel={() => void cancel(job.job_id)}
      />
    {/each}
    {#if data.truncated}<p class="muted">Showing the most recent jobs.</p>{/if}
  {:else if !error}<p class="muted">Loading jobs…</p>{/if}
</section>

<style>
  .waiting {
    border-left: 3px solid #5284ba;
    padding: 0.7rem 1rem;
    background: #5284ba10;
  }
</style>
