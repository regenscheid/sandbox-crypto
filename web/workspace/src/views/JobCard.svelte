<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import type { BackgroundJob } from '../lib/api';
  import {
    canRelease,
    collectionText,
    cpuText,
    deadlineText,
    finishedStates,
    jobElapsed,
    manifestText,
    memoryText,
    phaseText,
    sandboxTag,
  } from '../lib/jobs';
  let {
    job,
    cancelling = false,
    oncancel,
    releasing = false,
    onrelease,
  }: {
    job: BackgroundJob;
    cancelling?: boolean;
    oncancel?: () => void;
    releasing?: boolean;
    onrelease?: () => void;
  } = $props();
  const tag = $derived(sandboxTag(job));
</script>

<article class="job-card" aria-label={`Job ${job.job_id}`}>
  <div class="job-heading">
    {#if tag}<span class="job-tag" title="Job sandbox: profile and phase">{tag}</span>{/if}
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
  {#if job.sandbox}
    {@const sandbox = job.sandbox}
    <section class="sandbox" aria-label="Sandbox">
      <h4>Sandbox</h4>
      <dl>
        <dt>Profile</dt>
        <dd>{sandbox.profile}</dd>
        <dt>Phase</dt>
        <dd>{phaseText(sandbox.phase)}</dd>
        <dt>Environment</dt>
        <dd><code>{sandbox.environment_id}</code></dd>
        <dt>Requested</dt>
        <dd>{cpuText(sandbox.cpu_millicores)}, {memoryText(sandbox.memory_mib)} memory</dd>
        <dt>Collection</dt>
        <dd>{collectionText(job)}</dd>
        {#if sandbox.phase === 'retained' && sandbox.retained_until}
          <dt>Retained until</dt>
          <dd>{deadlineText(sandbox.retained_until)}</dd>
        {/if}
      </dl>
      {#if job.manifest}
        {@const manifest = job.manifest}
        <p class="muted manifest">Manifest: {manifestText(manifest)}</p>
        {#if manifest.not_collected.length}
          <details>
            <summary>Files not collected</summary>
            <ul>
              {#each manifest.not_collected as left}
                <li><code>{left.path}</code>: {left.reason}</li>
              {/each}
            </ul>
          </details>
        {/if}
      {/if}
      {#if canRelease(job) && onrelease}
        <button class="release" disabled={releasing} onclick={onrelease}>
          {releasing ? 'Releasing…' : 'Release now'}
        </button>
      {/if}
    </section>
  {/if}
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
  .job-heading button {
    margin-left: auto;
  }
  .job-tag {
    font-size: 0.85rem;
    padding: 0.1rem 0.5rem;
    border-radius: 0.3rem;
    background: var(--surface-muted, #8883);
    white-space: nowrap;
  }
  .sandbox {
    border-top: 1px solid var(--border, #ccd);
    margin-top: 0.6rem;
  }
  dl {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.2rem 1rem;
    margin: 0.4rem 0;
  }
  dt {
    color: var(--muted, #667);
  }
  dd {
    margin: 0;
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
