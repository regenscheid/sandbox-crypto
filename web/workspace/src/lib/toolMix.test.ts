// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { RunRecord } from './api';
import { toolMixLine } from './transcript';
import RunView from '../views/RunView.svelte';

const mix = {
  classifier: 'shell-computation-1',
  python: 10,
  sage: 2,
  shell: 7,
  shell_computation: 3,
  start_job: 1,
  restart_kernel: 0,
  state_lost: 1,
};

const run: RunRecord = {
  run_id: 'r1',
  context: {
    project_id: 'p',
    session_id: 's',
    environment_id: 'e',
    policy_revision: null,
    actor: 'agent',
  },
  task: 'Compute',
  started_at: '2026-10-25T13:00:00Z',
  ended_at: '2026-10-25T13:05:00Z',
  stopped: 'answered',
  final_text: 'Done',
  messages: [{ role: 'assistant', content: 'Done' }],
  tools: [],
  summaries: { tool_mix: mix },
};

describe("a run's tool mix", () => {
  it('words the counts in one line', () => {
    expect(toolMixLine({ tool_mix: mix })).toBe(
      '12 cells (python 10, sage 2) · 7 shell calls, 3 ran Python · 1 job · kernel state lost once',
    );
  });
  it('names restarts and repeated losses, and leaves out what did not happen', () => {
    expect(
      toolMixLine({
        tool_mix: {
          python: 1,
          sage: 0,
          shell: 0,
          shell_computation: 0,
          restart_kernel: 2,
          state_lost: 3,
        },
      }),
    ).toBe('1 cell (python 1) · 0 shell calls · 2 kernel restarts · kernel state lost 3 times');
    expect(toolMixLine({ tool_mix: { python: 0, shell: 1, state_lost: 2 } })).toBe(
      '0 cells · 1 shell call · kernel state lost twice',
    );
  });
  it('shows nothing for a record without one, or with no such calls', () => {
    expect(toolMixLine(undefined)).toBeNull();
    expect(toolMixLine({ skills: {} })).toBeNull();
    expect(toolMixLine({ tool_mix: 'stored' })).toBeNull();
    expect(toolMixLine({ tool_mix: { python: 0, shell: 0 } })).toBeNull();
  });
  it("is in the run view's facts", () => {
    const html = render(RunView, { props: { run, runName: 'saved' } }).body;
    expect(html).toContain('Computation');
    expect(html).toContain('12 cells (python 10, sage 2) · 7 shell calls, 3 ran Python');
    const { summaries: _summaries, ...older } = run;
    expect(render(RunView, { props: { run: older, runName: 'saved' } }).body).not.toContain(
      'Computation',
    );
  });
});
