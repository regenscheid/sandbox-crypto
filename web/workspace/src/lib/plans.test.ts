// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  planCounts,
  planPanelScope,
  readSessionPlan,
  type PlanAuthor,
  type PlanObservation,
} from './plans';
import type { PollState } from './poller';
import PlanPanel from '../views/PlanPanel.svelte';

const scope = { projectId: 'p', sessionId: 's' };
const at = '2026-10-13T00:40:00.123456+00:00';
function author(): PlanAuthor {
  return {
    scope: { project_id: 'p', session_id: 's' },
    role: 'main',
    user_id: null,
    run_id: 'run-one',
    alias: 'Researcher',
    resolved_model: 'test-model',
    tool_call: 'call-one',
    grant: null,
    track_id: null,
  };
}
function observed(): PlanObservation {
  const ref = { plan_id: 'plan-one', version: 1 };
  return {
    project_id: 'p',
    session_id: 's',
    observed_at: at,
    session_closed_at: null,
    plan: {
      event_cursor: 3,
      head: {
        scope: { project_id: 'p', session_id: 's' },
        ref,
        status_revision: 2,
        progress_cursor: 1,
        status: 'active',
        outcome: null,
      },
      version: {
        scope: { project_id: 'p', session_id: 's' },
        ref,
        base: null,
        author: author(),
        reason: 'Initial plan',
        recorded_at: at,
        body: {
          goal: 'Study the construction',
          feasibility: 'Use the provided evidence',
          outputs: [
            {
              title: 'Report',
              description: 'A bounded result',
              evidence_standard: 'Retained execution',
            },
          ],
          tracks: [],
          phases: [
            {
              phase_id: 'analysis',
              title: 'Analysis',
              steps: [
                {
                  step_id: 'inspect',
                  title: 'Inspect evidence',
                  description: 'Read the current evidence',
                  evidence_standard: 'Exact source',
                  track_id: null,
                },
                {
                  step_id: 'validate',
                  title: 'Validate result',
                  description: 'Check the witness',
                  evidence_standard: 'Reproducible result',
                  track_id: null,
                },
              ],
            },
          ],
        },
      },
      steps: [
        {
          step_id: 'inspect',
          step_sha256: 'a'.repeat(64),
          matches_current_step: true,
          latest: {
            ref,
            sequence: 1,
            step_sha256: 'a'.repeat(64),
            status: 'in_progress',
            author: author(),
            recorded_at: at,
            evidence_count: 0,
          },
        },
        {
          step_id: 'validate',
          step_sha256: 'b'.repeat(64),
          matches_current_step: false,
          latest: null,
        },
      ],
    },
  };
}
function response(value: unknown = observed()) {
  return new Response(JSON.stringify(value));
}
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('plan views', () => {
  it('counts only matching current-step progress and qualifies terminal goal outcome', () => {
    const value = observed(),
      plan = value.plan!;
    plan.steps[0]!.latest!.status = 'completed';
    plan.steps[0]!.step_sha256 = 'c'.repeat(64);
    plan.steps[0]!.matches_current_step = false;
    expect(planCounts(plan)).toMatchObject({ completed: 0, pending: 2 });
    plan.steps[0]!.step_sha256 = 'a'.repeat(64);
    plan.steps[0]!.matches_current_step = true;
    plan.steps[0]!.latest!.status = 'blocked';
    plan.steps[1]!.latest = {
      ...plan.steps[0]!.latest!,
      sequence: 2,
      step_sha256: 'b'.repeat(64),
      status: 'skipped',
    };
    plan.steps[1]!.matches_current_step = true;
    plan.head.progress_cursor = 2;
    plan.head.status = 'completed';
    plan.head.outcome = {
      status: 'partially_achieved',
      summary: 'Some evidence is missing',
      evidence: [],
      remaining_work: ['Obtain missing evidence'],
    };
    expect(planCounts(plan)).toMatchObject({ blocked: 1, skipped: 1 });
  });
  it('reads the plan route and refuses a plan of another session', async () => {
    const fetch = vi.fn().mockResolvedValue(response());
    vi.stubGlobal('fetch', fetch);
    expect(await readSessionPlan(scope, new AbortController().signal)).toEqual(observed());
    expect(fetch.mock.calls[0]?.[0]).toBe('/api/v0/projects/p/sessions/s/plan');
    fetch.mockResolvedValue(response({ ...observed(), session_id: 'other' }));
    await expect(readSessionPlan(scope, new AbortController().signal)).rejects.toMatchObject({
      status: 0,
    });
  });
  it('mounts the panel only for the loaded session', () => {
    const detail = { project_id: 'p', session: { session_id: 's' } };
    expect(planPanelScope('p', 's', detail)).toEqual(scope);
    expect(planPanelScope('p', 's', { ...detail, project_id: 'q' })).toBeNull();
  });
});

describe('plan panel rendering', () => {
  const html = (state: Omit<PollState<PlanObservation>, 'busy'>) =>
    render(PlanPanel, { props: { ...scope, initialState: { ...state, busy: false } } }).body;
  it('renders recorded progress/stable IDs with SSR edit controls disabled and no request', () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    const text = html({ phase: 'observed', data: observed() });
    expect(text).toContain('Study the construction');
    expect(text).toContain('0 of 2 steps completed');
    expect(text).toContain('1 in progress');
    expect(text).toContain('Inspect evidence');
    expect(text).toContain('inspect');
    expect(text).toContain('Researcher');
    expect(text).toContain('does not prove a job is running');
    expect(text).toContain('<fieldset disabled');
    expect(text).toContain('type="submit" disabled');
    expect(text).toContain('Saving does not approve the plan');
    expect(fetch).not.toHaveBeenCalled();
  });
  it('shows revised steps pending and labels previous completed assertions', () => {
    const value = observed();
    value.plan!.steps[0]!.latest!.status = 'completed';
    value.plan!.steps[0]!.matches_current_step = false;
    const text = html({ phase: 'observed', data: value });
    expect(text).toContain('0 of 2 steps completed');
    expect(text).toContain('Previous step version was completed');
    expect(text).toContain('needs a new progress update');
  });
  it('distinguishes known no-plan from unavailable and previous data, and escapes prose', () => {
    expect(html({ phase: 'observed', data: { ...observed(), plan: null } })).toContain(
      'No plan has been recorded',
    );
    expect(html({ phase: 'unavailable', data: null })).toContain('current plan state is unknown');
    expect(html({ phase: 'unavailable', data: observed() })).toContain(
      'Previous observation; current plan state is unknown',
    );
    const value = observed();
    value.plan!.version.body.goal = '<img src=x onerror=alert(1)>';
    const text = html({ phase: 'observed', data: value });
    expect(text).not.toContain('<img');
    expect(text).toContain('&lt;img');
  });
});
