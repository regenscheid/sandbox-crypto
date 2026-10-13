// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';
import { PLAN_DEADLINE_MS, PLAN_MAX_BYTES, PlanPoller, PlanReadError, planObservation, planCounts, planPanelScope,
  readSessionPlan, onPlanLogout, type PlanAuthor, type PlanObservation, type PlanPollState } from './plans';
import PlanPanel from '../views/PlanPanel.svelte';

const scope = {projectId: 'p', sessionId: 's'};
const at = '2026-10-13T00:40:00.123456+00:00';
function author(): PlanAuthor { return {scope: {project_id: 'p', session_id: 's'}, role: 'main', user_id: null, run_id: 'run-one', alias: 'Researcher', resolved_model: 'test-model', tool_call: 'call-one', grant: null, track_id: null}; }
function observed(): PlanObservation {
  const ref = {plan_id: 'plan-one', version: 1};
  return {project_id: 'p', session_id: 's', observed_at: at, session_closed_at: null, plan: {
    event_cursor: 3,
    head: {scope: {project_id: 'p', session_id: 's'}, ref, status_revision: 2, progress_cursor: 1, status: 'active', outcome: null},
    version: {scope: {project_id: 'p', session_id: 's'}, ref, base: null, author: author(), reason: 'Initial plan', recorded_at: at,
      body: {goal: 'Study the construction', feasibility: 'Use the provided evidence', outputs: [{title: 'Report', description: 'A bounded result', evidence_standard: 'Retained execution'}], tracks: [],
        phases: [{phase_id: 'analysis', title: 'Analysis', steps: [
          {step_id: 'inspect', title: 'Inspect evidence', description: 'Read the current evidence', evidence_standard: 'Exact source', track_id: null},
          {step_id: 'validate', title: 'Validate result', description: 'Check the witness', evidence_standard: 'Reproducible result', track_id: null},
        ]}]}},
    steps: [{step_id: 'inspect', step_sha256: 'a'.repeat(64), matches_current_step: true, latest: {ref, sequence: 1, step_sha256: 'a'.repeat(64), status: 'in_progress', author: author(), recorded_at: at, evidence_count: 0}},
      {step_id: 'validate', step_sha256: 'b'.repeat(64), matches_current_step: false, latest: null}],
  }};
}
function deferred<T>() { let resolve!: (v: T) => void; const promise = new Promise<T>(yes => {resolve = yes;}); return {promise, resolve}; }
function response(value: unknown = observed()) { return new Response(JSON.stringify(value)); }
const pollers: PlanPoller[] = [];
function poll(read: (signal: AbortSignal) => Promise<PlanObservation>, once = false) {
  const states: PlanPollState[] = [], reader = new PlanPoller(read, state => states.push(state), once); pollers.push(reader); return {reader, states};
}
afterEach(() => {for (const reader of pollers.splice(0)) reader.stop(); vi.useRealTimers(); vi.unstubAllGlobals();});

describe('scoped compact plan validation', () => {
  it('accepts exact current pins, Unicode prose, empty and closed history', () => {
    const value = observed(); value.plan!.version.body.goal = 'Study 🔑 and α';
    expect(planObservation(value, scope)).toEqual(value);
    expect(planObservation({...value, plan: null, session_closed_at: at}, scope).plan).toBeNull();
    expect(planPanelScope('p', 's', {project_id: 'p', session: {session_id: 's'}})).toEqual(scope);
    expect(planPanelScope('p', 's', {project_id: 'q', session: {session_id: 's'}})).toBeNull();
  });
  it.each([
    ['project', (v: PlanObservation) => {v.project_id = 'q';}],
    ['session', (v: PlanObservation) => {v.session_id = 'other';}],
    ['head scope', (v: PlanObservation) => {v.plan!.head.scope.project_id = 'q';}],
    ['version scope', (v: PlanObservation) => {v.plan!.version.scope.session_id = 'other';}],
    ['author scope', (v: PlanObservation) => {v.plan!.version.author.scope.project_id = 'q';}],
    ['progress author', (v: PlanObservation) => {v.plan!.steps[0]!.latest!.author.scope.session_id = 'other';}],
    ['head ref', (v: PlanObservation) => {v.plan!.head.ref = {plan_id: 'other', version: 1};}],
    ['future progress ref', (v: PlanObservation) => {v.plan!.steps[0]!.latest!.ref = {plan_id: 'plan-one', version: 2};}],
    ['foreign progress ref', (v: PlanObservation) => {v.plan!.steps[0]!.latest!.ref = {plan_id: 'other', version: 1};}],
    ['future sequence', (v: PlanObservation) => {v.plan!.steps[0]!.latest!.sequence = 2;}],
    ['false fingerprint match', (v: PlanObservation) => {v.plan!.steps[0]!.step_sha256 = 'c'.repeat(64);}],
    ['unproved empty match', (v: PlanObservation) => {v.plan!.steps[1]!.matches_current_step = true;}],
    ['missing step', (v: PlanObservation) => {v.plan!.steps.pop();}],
    ['wrong order', (v: PlanObservation) => {v.plan!.steps.reverse();}],
    ['duplicate ID', (v: PlanObservation) => {v.plan!.version.body.phases[0]!.steps[1]!.step_id = 'inspect';}],
    ['missing track', (v: PlanObservation) => {v.plan!.version.body.phases[0]!.steps[0]!.track_id = 'missing';}],
    ['unsafe cursor', (v: PlanObservation) => {v.plan!.event_cursor = 2 ** 53;}],
    ['fractional cursor', (v: PlanObservation) => {v.plan!.head.progress_cursor = 0.5;}],
    ['invalid date', (v: PlanObservation) => {v.observed_at = '2026-02-31T00:00:00Z';}],
    ['missing zone', (v: PlanObservation) => {v.observed_at = '2026-10-13T00:00:00';}],
    ['oversized prose', (v: PlanObservation) => {v.plan!.version.body.goal = 'x'.repeat(4097);}],
    ['NUL prose', (v: PlanObservation) => {v.plan!.version.body.goal = 'x\0y';}],
    ['lone surrogate', (v: PlanObservation) => {v.plan!.version.body.goal = '\uD800';}],
    ['forged USER', (v: PlanObservation) => {v.plan!.version.author.role = 'user';}],
    ['missing goal outcome', (v: PlanObservation) => {v.plan!.head.status = 'completed';}],
  ])('rejects %s', (_name, mutate) => { const value = observed(); mutate(value); expect(() => planObservation(value, scope)).toThrow(PlanReadError); });
  it('counts only matching current-step progress and qualifies terminal goal outcome', () => {
    const value = observed(), plan = value.plan!;
    plan.steps[0]!.latest!.status = 'completed'; plan.steps[0]!.step_sha256 = 'c'.repeat(64); plan.steps[0]!.matches_current_step = false;
    expect(planObservation(value, scope)).toEqual(value); expect(planCounts(plan)).toMatchObject({completed: 0, pending: 2});
    plan.steps[0]!.step_sha256 = 'a'.repeat(64); plan.steps[0]!.matches_current_step = true; plan.steps[0]!.latest!.status = 'blocked';
    plan.steps[1]!.latest = {...plan.steps[0]!.latest!, sequence: 2, step_sha256: 'b'.repeat(64), status: 'skipped'};
    plan.steps[1]!.matches_current_step = true; plan.head.progress_cursor = 2; plan.head.status = 'completed';
    plan.head.outcome = {status: 'partially_achieved', summary: 'Some evidence is missing', evidence: [], remaining_work: ['Obtain missing evidence']};
    expect(planObservation(value, scope)).toEqual(value);
    plan.head.outcome.status = 'achieved'; plan.head.outcome.remaining_work = [];
    expect(() => planObservation(value, scope)).toThrow(PlanReadError);
  });
});

describe('owned bounded plan HTTP reads', () => {
  it('uses the exact scoped same-origin read without redirects or cache', async () => {
    const fetch = vi.fn().mockResolvedValue(response()); vi.stubGlobal('fetch', fetch);
    expect(await readSessionPlan(scope, new AbortController().signal)).toEqual(observed());
    expect(fetch.mock.calls[0]).toMatchObject(['/api/v0/projects/p/sessions/s/plan', {redirect: 'error', cache: 'no-store', headers: {Accept: 'application/json'}}]);
  });
  it.each([401, 404, 503])('cancels arbitrary %i error bodies without parsing', async status => {
    const cancel = vi.fn(); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({cancel}), {status})));
    await expect(readSessionPlan(scope, new AbortController().signal)).rejects.toMatchObject({status, message: 'Session plan unavailable'}); expect(cancel).toHaveBeenCalledOnce();
  });
  it('refuses declared/streamed oversized bodies, excessive chunks and invalid UTF8', async () => {
    for (const kind of ['declared', 'bytes', 'chunks', 'utf8']) {
      const cancel = vi.fn();
      const stream = new ReadableStream<Uint8Array>({pull(controller) {
        controller.enqueue(kind === 'utf8' ? new Uint8Array([255]) : new Uint8Array(kind === 'bytes' ? PLAN_MAX_BYTES + 1 : 0));
        if (kind === 'utf8') controller.close();
      }, cancel});
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, {headers: kind === 'declared' ? {'content-length': String(PLAN_MAX_BYTES + 1)} : {}})));
      await expect(readSessionPlan(scope, new AbortController().signal)).rejects.toBeInstanceOf(PlanReadError);
      if (kind !== 'utf8') expect(cancel).toHaveBeenCalledOnce();
    }
  });
  it('holds ownership across ignored header abort and refuses late results', async () => {
    vi.useFakeTimers(); const pending = deferred<Response>(), fetch = vi.fn().mockReturnValue(pending.promise); vi.stubGlobal('fetch', fetch);
    const first = readSessionPlan(scope, new AbortController().signal).catch(error => error);
    await vi.advanceTimersByTimeAsync(PLAN_DEADLINE_MS);
    expect(fetch.mock.calls[0]?.[1].signal.aborted).toBe(true);
    await expect(readSessionPlan(scope, new AbortController().signal)).rejects.toMatchObject({status: 0}); expect(fetch).toHaveBeenCalledOnce();
    pending.resolve(response()); expect(await first).toBeInstanceOf(PlanReadError);
    fetch.mockResolvedValue(response()); await expect(readSessionPlan(scope, new AbortController().signal)).resolves.toEqual(observed());
  });
  it('owns cancellation until a stalled body actually drains', async () => {
    vi.useFakeTimers(); const draining = deferred<void>(), cancel = vi.fn().mockReturnValue(draining.promise);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream<Uint8Array>({cancel}))));
    const first = readSessionPlan(scope, new AbortController().signal).catch(error => error);
    await vi.advanceTimersByTimeAsync(PLAN_DEADLINE_MS); expect(cancel).toHaveBeenCalledOnce();
    await expect(readSessionPlan(scope, new AbortController().signal)).rejects.toMatchObject({status: 0});
    draining.resolve(); expect(await first).toBeInstanceOf(PlanReadError);
  });
});

describe('plan polling lifecycle', () => {
  it('polls visible pages, pauses hidden, and never overlaps an ignored abort', async () => {
    vi.useFakeTimers(); const pending = deferred<PlanObservation>(), read = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue(observed()), {reader, states} = poll(read);
    reader.setVisible(true); reader.setVisible(false); reader.setVisible(true);
    await vi.advanceTimersByTimeAsync(30_000); expect(read).toHaveBeenCalledOnce();
    pending.resolve(observed()); await vi.advanceTimersByTimeAsync(0); expect(read).toHaveBeenCalledTimes(2);
    expect(states.filter(s => s.phase === 'observed')).toHaveLength(1);
    reader.setVisible(false); await vi.advanceTimersByTimeAsync(60_000); expect(read).toHaveBeenCalledTimes(2); expect(states.at(-1)?.phase).toBe('paused');
  });
  it('retains dated prior observations on unavailability, clears missing scope', async () => {
    vi.useFakeTimers(); const read = vi.fn().mockResolvedValueOnce(observed()).mockRejectedValueOnce(new PlanReadError(503)).mockRejectedValue(new PlanReadError(404));
    const {reader, states} = poll(read); reader.setVisible(true); await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(3000); expect(states.at(-1)).toEqual({phase: 'unavailable', data: observed()});
    await vi.advanceTimersByTimeAsync(6000); expect(states.at(-1)).toEqual({phase: 'missing', data: null});
    await vi.advanceTimersByTimeAsync(60_000); expect(read).toHaveBeenCalledTimes(3);
  });
  it('reads closed history once and stops after a close observed during polling', async () => {
    vi.useFakeTimers();
    for (const once of [true, false]) {
      const read = vi.fn().mockResolvedValue({...observed(), session_closed_at: once ? null : at}), {reader, states} = poll(read, once);
      reader.setVisible(true); await vi.advanceTimersByTimeAsync(60_000); expect(read).toHaveBeenCalledOnce(); expect(states.at(-1)?.phase).toBe('observed');
      reader.setVisible(false); reader.setVisible(true); await vi.advanceTimersByTimeAsync(60_000); expect(read).toHaveBeenCalledOnce();
    }
  });
  it('does not retry a closed-history failure or an ignored deadline abort', async () => {
    vi.useFakeTimers();
    const failed = vi.fn().mockRejectedValue(new PlanReadError(503)), first = poll(failed, true);
    first.reader.setVisible(true); await vi.advanceTimersByTimeAsync(60_000);
    expect(failed).toHaveBeenCalledOnce(); expect(first.states.at(-1)?.phase).toBe('unavailable');
    const pending = deferred<PlanObservation>(), read = vi.fn().mockReturnValue(pending.promise), second = poll(read, true);
    second.reader.setVisible(true); await vi.advanceTimersByTimeAsync(PLAN_DEADLINE_MS);
    expect(second.states.at(-1)?.phase).toBe('unavailable'); expect(read).toHaveBeenCalledOnce();
    pending.resolve(observed()); await vi.advanceTimersByTimeAsync(60_000);
    expect(second.states.at(-1)?.phase).toBe('unavailable'); expect(read).toHaveBeenCalledOnce();
    second.reader.setVisible(false); second.reader.setVisible(true); await vi.advanceTimersByTimeAsync(60_000); expect(read).toHaveBeenCalledOnce();
  });
  it('sign-out stops all future reads and late results even if POST fails', async () => {
    vi.useFakeTimers(); const pending = deferred<PlanObservation>(), {reader, states} = poll(() => pending.promise), off = onPlanLogout(() => reader.logout());
    reader.setVisible(true); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', {status: 503})));
    await expect(api.logout()).rejects.toThrow(); expect(states.at(-1)).toEqual({phase: 'signed_out', data: null});
    pending.resolve(observed()); await vi.advanceTimersByTimeAsync(60_000); expect(states.at(-1)?.phase).toBe('signed_out');
    await expect(readSessionPlan(scope, new AbortController().signal)).rejects.toMatchObject({status: 401}); off();
  });
});

describe('plan panel rendering', () => {
  const html = (state: PlanPollState) => render(PlanPanel, {props: {...scope, initialState: state}}).body;
  it('renders recorded progress/stable IDs with no SSR request or mutation controls', () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch); const text = html({phase: 'observed', data: observed()});
    expect(text).toContain('Study the construction'); expect(text).toContain('0 of 2 steps completed'); expect(text).toContain('1 in progress');
    expect(text).toContain('Inspect evidence'); expect(text).toContain('inspect'); expect(text).toContain('Researcher');
    expect(text).toContain('does not prove a job is running'); expect(text).not.toContain('<button'); expect(fetch).not.toHaveBeenCalled();
  });
  it('shows revised steps pending and labels previous completed assertions', () => {
    const value = observed(); value.plan!.steps[0]!.latest!.status = 'completed'; value.plan!.steps[0]!.matches_current_step = false;
    const text = html({phase: 'observed', data: value}); expect(text).toContain('0 of 2 steps completed'); expect(text).toContain('Previous step version was completed');
    expect(text).toContain('needs a new progress update');
  });
  it('distinguishes known no-plan from unavailable and previous data, and escapes prose', () => {
    expect(html({phase: 'observed', data: {...observed(), plan: null}})).toContain('No plan has been recorded');
    expect(html({phase: 'unavailable', data: null})).toContain('current plan state is unknown');
    expect(html({phase: 'unavailable', data: observed()})).toContain('Previous observation; current plan state is unknown');
    const value = observed(); value.plan!.version.body.goal = '<img src=x onerror=alert(1)>';
    const text = html({phase: 'observed', data: value}); expect(text).not.toContain('<img'); expect(text).toContain('&lt;img');
  });
});
