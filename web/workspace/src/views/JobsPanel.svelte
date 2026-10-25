<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { api, type BackgroundJob, type JobListing } from '../lib/api';
  import { JobCancels, jobElapsed, jobsPoller, releaseNow } from '../lib/jobs';
  import { initialPoll, type PollState } from '../lib/poller';
  import { idle, submit } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  import JobCard from './JobCard.svelte';
  import PollStatus from './PollStatus.svelte';

  let { projectId, sessionId }: { projectId: string; sessionId: string } = $props();
  let view = $state<PollState<JobListing>>(initialPoll());
  const cancel = $state({ queued: [] as string[], error: null as unknown });
  const cancels = new JobCancels(cancel, async (job) => {
    await api.cancelJob(projectId, sessionId, job);
    const found = await api.jobs(projectId, sessionId);
    // Spread the state as it is now: the poller may have published during the await.
    view = { ...view, data: found };
  });
  const data = $derived(view.data);
  const release = $state({ ...idle(), job: null as string | null });

  async function reread(): Promise<void> {
    // Spread the state as it is now: the poller may have published during the await.
    view = { ...view, data: await api.jobs(projectId, sessionId) };
  }

  /** "Release now" for a retained job sandbox, after the user confirms. The list is read
   * again after the answer or a refusal, since the sandbox may have been released already. */
  async function releaseSandbox(job: BackgroundJob): Promise<void> {
    release.job = job.job_id;
    try {
      await submit(
        release,
        async () => {
          const sent = await releaseNow(
            `${projectId}:${sessionId}`,
            job,
            (question) => confirm(question),
            (body) => api.releaseJobSandbox(projectId, sessionId, job.job_id, body),
          );
          if (sent) await reread();
        },
        reread,
      );
    } finally {
      release.job = null;
    }
  }

  $effect(() => {
    const project = projectId,
      session = sessionId;
    view = initialPoll();
    cancel.error = null;
    release.error = null;
    return jobsPoller(project, session, (value) => {
      view = value;
    }).watch();
  });
</script>

<section aria-label="Background jobs">
  <h2>Background jobs</h2>
  {#if cancel.error}<ErrorMessage error={cancel.error} />{/if}
  {#if release.error}<ErrorMessage error={release.error} />{/if}
  <PollStatus phase={view.phase} hasData={data !== null} what="Job views" />
  {#if data}
    {#each data.waits as wait (wait.run_id)}
      <div class="waiting" role="status">
        <strong>{wait.connected ? 'Agent waiting' : 'Waiting harness disconnected'}</strong>
        for {wait.mode === 'all'
          ? 'all jobs'
          : wait.mode === 'first'
            ? 'the first job to end'
            : 'jobs'}:
        {wait.job_ids.join(', ')} · {wait.started_at
          ? jobElapsed((Date.now() - Date.parse(wait.started_at)) / 1000)
          : 'start time unavailable'}
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
        cancelling={cancel.queued.includes(job.job_id)}
        oncancel={() => void cancels.ask(job.job_id)}
        releasing={release.busy && release.job === job.job_id}
        onrelease={() => void releaseSandbox(job)}
      />
    {/each}
    {#if data.truncated}<p class="muted">Showing the most recent jobs.</p>{/if}
  {/if}
</section>

<style>
  .waiting {
    border-left: 3px solid #5284ba;
    padding: 0.7rem 1rem;
    background: #5284ba10;
  }
</style>
