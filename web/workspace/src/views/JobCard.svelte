<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import type { BackgroundJob } from '../lib/api';
  import { finishedStates, jobElapsed } from '../lib/jobs';
  let {
    job,
    cancelling = false,
    oncancel,
  }: {
    job: BackgroundJob;
    cancelling?: boolean;
    oncancel?: () => void;
  } = $props();
</script>

<article class="job-card" aria-label={`Job ${job.job_id}`}>
  <div class="job-heading">
    <strong>{job.purpose}</strong>
    <span class="job-state">{job.state}</span>
    <span class="muted">{jobElapsed(job.elapsed_seconds)}</span>
    {#if !finishedStates.has(job.state) && oncancel}
      <button disabled={cancelling} onclick={oncancel}>
        {cancelling ? 'Cancelling…' : 'Cancel job'}
      </button>
    {/if}
  </div>
  <p class="muted">{job.expected_progress}</p>
  <p class="muted">
    <code>{job.job_id}</code>
    {#if job.exit_code !== null}
      · exit {job.exit_code}{/if}
    {#if job.cpu_seconds !== null}
      · CPU {jobElapsed(job.cpu_seconds)}{/if}
    {#if job.memory_bytes !== null}
      · {(job.memory_bytes / 1048576).toFixed(1)} MiB{/if}
  </p>
  {#if job.checkin}
    <p class="checkin" role="status">
      <strong>{job.checkin.state === 'fine' ? 'Progress' : 'Needs attention'}:</strong>
      {job.checkin.note} <span class="muted">({job.checkin.alias})</span>
    </p>
  {/if}
  {#if job.error}<p class="warning">{job.error}</p>{/if}
  {#if job.stdout || job.stderr}
    <details>
      <summary>Output head and latest tail</summary>
      {#if job.stdout}<h4>stdout</h4>
        <pre>{job.stdout}</pre>{/if}
      {#if job.stderr}<h4>stderr</h4>
        <pre>{job.stderr}</pre>{/if}
    </details>
  {/if}
  {#if job.state === 'lost'}
    <p class="warning">The worker lost this job. It was not run again.</p>
  {/if}
</article>

<style>
  .job-card {
    border: 1px solid var(--border, #ccd);
    border-radius: 0.5rem;
    padding: 1rem;
    margin: 0.8rem 0;
  }
  .job-heading {
    display: flex;
    flex-wrap: wrap;
    gap: 0.8rem;
    align-items: center;
  }
  .job-state {
    font-size: 0.85rem;
    padding: 0.1rem 0.5rem;
    border: 1px solid currentColor;
    border-radius: 1rem;
  }
  button {
    margin-left: auto;
  }
  pre {
    max-height: 22rem;
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  h4 {
    margin-bottom: 0.4rem;
  }
</style>
