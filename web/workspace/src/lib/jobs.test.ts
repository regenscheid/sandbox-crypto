// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import JobCard from '../views/JobCard.svelte';
import PollStatus from '../views/PollStatus.svelte';
import type { BackgroundJob, JobListing } from './api';
import {
  JobCancels,
  RELEASE_QUESTION,
  canRelease,
  cpuText,
  deadlineText,
  jobElapsed,
  jobsPoller,
  memoryText,
  phaseText,
  releaseNow,
  sandboxTag,
} from './jobs';
import type { PollPhase, PollState } from './poller';

const job: BackgroundJob = {
  error: null,
  written_files: [],
  written_files_truncated: false,
  platform_notices: [],
  checkin: null,
  job_id: 'exe-1',
  state: 'running',
  purpose: '<script>attack</script>',
  expected_progress: 'progress once a minute',
  elapsed_seconds: 3601,
  exit_code: null,
  stdout: '<img src=x onerror=attack()>',
  stderr: 'ordinary error',
  cpu_seconds: 120,
  memory_bytes: 1048576,
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
    const html = render(JobCard, {
      props: { job: { ...job, state: 'lost' }, oncancel: () => undefined },
    }).body;
    expect(html).not.toContain('Cancel job');
    expect(html).toContain('It was not run again.');
  });
});

it('formats elapsed time at boundaries', () => {
  expect(jobElapsed(-1)).toBe('0s');
  expect(jobElapsed(60)).toBe('1m 0s');
  expect(jobElapsed(3600)).toBe('1h 0m');
});

describe('job listing polls', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });
  it('keep reading through worker refusals passed through as 404 or 409', async () => {
    vi.useFakeTimers();
    const listing: JobListing = { enabled: true, jobs: [], truncated: false, waits: [] };
    const answers = [
      new Response(JSON.stringify({ error: { kind: 'not_found', message: 'no environment' } }), {
        status: 404,
      }),
      new Response(JSON.stringify({ error: { kind: 'conflict', message: 'x' } }), {
        status: 409,
      }),
      new Response(JSON.stringify(listing)),
    ];
    const fetch = vi.fn(async () => answers.shift() ?? new Response(JSON.stringify(listing)));
    vi.stubGlobal('fetch', fetch);
    const states: PollState<JobListing>[] = [];
    const poller = jobsPoller('p', 's', (state) => states.push(state));
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(20_000);
    poller.stop();
    expect(fetch.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(states.filter((s) => !s.busy).at(-1)).toEqual({
      phase: 'observed',
      data: listing,
      busy: false,
    });
  });
  it.each<PollPhase>(['paused', 'unavailable', 'unsupported', 'missing', 'changed', 'signed_out'])(
    'say something for the %s phase',
    (phase) => {
      const html = render(PollStatus, { props: { phase, hasData: true, what: 'Job views' } }).body;
      expect(html.replace(/<!--[^>]*-->/g, '').trim()).not.toBe('');
    },
  );
});

describe('cancelling jobs', () => {
  function cancels() {
    const asked: string[] = [];
    const answers: { resolve: () => void; reject: (e: unknown) => void }[] = [];
    const state = { queued: [] as string[], error: null as unknown };
    const queue = new JobCancels(state, (job) => {
      asked.push(job);
      return new Promise<void>((resolve, reject) => answers.push({ resolve, reject }));
    });
    return { asked, answers, state, queue };
  }
  const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

  it('cancels a job asked for during another cancellation, after it, instead of dropping it', async () => {
    const { asked, answers, state, queue } = cancels();
    const first = queue.ask('job-a');
    void queue.ask('job-b');
    void queue.ask('job-b'); // asked twice, cancelled once
    expect(state.queued).toEqual(['job-a', 'job-b']); // both buttons show "Cancelling…"
    expect(asked).toEqual(['job-a']);
    answers[0]!.resolve();
    await settle();
    expect(asked).toEqual(['job-a', 'job-b']);
    answers[1]!.resolve();
    await first;
    expect(state).toEqual({ queued: [], error: null });
  });

  it('goes on after a failure and keeps it to show', async () => {
    const { asked, answers, state, queue } = cancels();
    const first = queue.ask('job-a');
    void queue.ask('job-b');
    const failure = new Error('worker unavailable');
    answers[0]!.reject(failure);
    await settle();
    answers[1]!.resolve();
    await first;
    expect(asked).toEqual(['job-a', 'job-b']);
    expect(state).toEqual({ queued: [], error: failure });
  });
});

const sandboxJob: BackgroundJob = {
  ...job,
  state: 'failed',
  sandbox: {
    profile: 'large',
    phase: 'retained',
    environment_id: 'p1-job-0123456789ab',
    cpu_millicores: 12000,
    memory_mib: 32768,
    collection: 'incomplete',
    retained_until: '2026-10-28T09:30:45+00:00',
    files: '.jobs/exe-1/files/',
    manifest_sha256: 'a'.repeat(64),
  },
  manifest: {
    reason: 'sandbox_lost',
    collected: 3,
    not_collected_total: 2,
    not_collected: [
      { path: '<b>big.bin</b>', reason: 'over the 100 MiB file limit' },
      { path: 'x', reason: 'unreadable' },
    ],
    replayed_packages: 4,
    isolation_tier: 3,
  },
};

describe('a job in a sandbox of its own', () => {
  it('shows its profile and phase in the list line and the whole sandbox block', () => {
    const html = render(JobCard, { props: { job: sandboxJob } }).body;
    expect(html).toContain('large · retained');
    expect(html).toContain('aria-label="Sandbox"');
    expect(html).toContain('p1-job-0123456789ab');
    expect(html).toContain('12 cores, 32 GiB memory');
    expect(html).toContain('incomplete · 3 files');
    expect(html).toContain('2026-10-28 09:30 UTC');
    expect(html).toContain('ended sandbox_lost');
    expect(html).toContain('2 not collected');
    expect(html).toContain('4 replayed packages');
    expect(html).toContain('isolation tier 3');
    // The names a job chose are text, never markup.
    expect(html).toContain('&lt;b>big.bin&lt;/b>');
    expect(html).not.toMatch(/<b>big/);
  });
  it('shows no retention deadline once released, and says a creating one is not placed yet', () => {
    const released = render(JobCard, {
      props: {
        job: { ...sandboxJob, sandbox: { ...sandboxJob.sandbox!, phase: 'released' } },
        onrelease: () => undefined,
      },
    }).body;
    expect(released).not.toContain('Retained until');
    expect(released).not.toContain('Release now');
    expect(phaseText('creating')).toContain('not placed yet');
    const creating = render(JobCard, {
      props: {
        job: {
          ...job,
          sandbox: { ...sandboxJob.sandbox!, phase: 'creating', retained_until: null },
          manifest: undefined,
        },
      },
    }).body;
    expect(creating).toContain('not placed yet');
    expect(creating).not.toContain('Manifest:');
  });
  it('offers Release now only on a retained sandbox, and no sandbox block to a plain job', () => {
    expect(
      render(JobCard, { props: { job: sandboxJob, onrelease: () => undefined } }).body,
    ).toContain('Release now');
    expect(render(JobCard, { props: { job: sandboxJob } }).body).not.toContain('Release now');
    const plain = render(JobCard, { props: { job, onrelease: () => undefined } }).body;
    expect(plain).not.toContain('Sandbox');
    expect(plain).not.toContain('Release now');
    expect(sandboxTag(job)).toBeNull();
    expect(sandboxTag(sandboxJob)).toBe('large · retained');
    expect(canRelease(job)).toBe(false);
  });
  it('formats the requested resources and deadlines', () => {
    expect(cpuText(1000)).toBe('1 core');
    expect(cpuText(500)).toBe('0.5 cores');
    expect(cpuText(12000)).toBe('12 cores');
    expect(memoryText(512)).toBe('512 MiB');
    expect(memoryText(1536)).toBe('1.5 GiB');
    expect(deadlineText('not a time')).toBe('not a time');
  });
});

describe('releasing a retained sandbox now', () => {
  it('asks first and sends nothing when the user declines', async () => {
    const send = vi.fn(async () => ({}));
    const ask = vi.fn(() => false);
    expect(await releaseNow('p:s', sandboxJob, ask, send)).toBe(false);
    expect(ask).toHaveBeenCalledWith(RELEASE_QUESTION);
    expect(send).not.toHaveBeenCalled();
  });
  it('sends one operation ID, kept for a retry after a failure, then a new one', async () => {
    const ids: string[] = [];
    let fail = true;
    const send = async (body: { operation_id: string }) => {
      ids.push(body.operation_id);
      if (fail) throw new Error('lost answer');
      return {};
    };
    await expect(releaseNow('p:s', sandboxJob, () => true, send)).rejects.toThrow('lost answer');
    fail = false;
    expect(await releaseNow('p:s', sandboxJob, () => true, send)).toBe(true);
    expect(await releaseNow('p:s', sandboxJob, () => true, send)).toBe(true);
    expect(ids[1]).toBe(ids[0]);
    expect(ids[2]).not.toBe(ids[0]);
  });
  it('does not ask about a sandbox that is not retained', async () => {
    const ask = vi.fn(() => true);
    const running = {
      ...sandboxJob,
      sandbox: { ...sandboxJob.sandbox!, phase: 'running' as const },
    };
    expect(await releaseNow('p:s', running, ask, vi.fn())).toBe(false);
    expect(ask).not.toHaveBeenCalled();
  });
});
