// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import JobCard from '../views/JobCard.svelte';
import type { BackgroundJob } from './api';
import { jobElapsed } from './jobs';

const job: BackgroundJob = {
  job_id: 'exe-1', state: 'running', purpose: '<script>attack</script>',
  expected_progress: 'progress once a minute', elapsed_seconds: 3601,
  exit_code: null, stdout: '<img src=x onerror=attack()>', stderr: 'ordinary error',
  cpu_seconds: 120, memory_bytes: 1048576,
  logs: { stdout: '.jobs/exe-1/stdout.log', stderr: '.jobs/exe-1/stderr.log' },
  worker_restart: 'running jobs become lost until the executor daemon is available',
};

describe('job card', () => {
  it('shows progress, status, resource use, bounded text and a cancel action', () => {
    const html = render(JobCard, { props: { job, oncancel: () => undefined } }).body;
    expect(html).toContain('running');
    expect(html).toContain('1h 0m');
    expect(html).toContain('1.0 MiB');
    expect(html).toContain('Cancel job');
    expect(html).toContain('Output head and latest tail');
    expect(html).toContain('&lt;script>attack&lt;/script>');
    expect(html).not.toMatch(/<(script|img)\b/);
  });
  it('does not offer to cancel ended jobs and explains lost execution', () => {
    const html = render(JobCard, { props: { job: { ...job, state: 'lost' }, oncancel: () => undefined } }).body;
    expect(html).not.toContain('Cancel job');
    expect(html).toContain('It was not run again.');
  });
});

it('formats elapsed time at boundaries', () => {
  expect(jobElapsed(-1)).toBe('0s');
  expect(jobElapsed(60)).toBe('1m 0s');
  expect(jobElapsed(3600)).toBe('1h 0m');
});
