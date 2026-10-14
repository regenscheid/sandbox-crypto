// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  PlanEditSession,
  PlanWriteError,
  EDIT_DEADLINE_MS,
  EDIT_RESPONSE_BYTES,
  editProblem,
  loadPlanDraft,
  sameStructure,
  textFields,
  writePlanChange,
  type EditorView,
  type PlanChange,
  type PlanReceipt,
} from './planEdits';
import type { PlanObservation, PlanScope } from './plans';
import PlanEditor from '../views/PlanEditor.svelte';
import PlanPanel from '../views/PlanPanel.svelte';
const scope = { projectId: 'p', sessionId: 's' },
  at = '2026-10-13T04:00:00.000000Z';
let index = 0;
function observed(plan = `plan-${++index}`): PlanObservation {
  const ref = { plan_id: plan, version: 1 };
  return {
    project_id: 'p',
    session_id: 's',
    observed_at: at,
    session_closed_at: null,
    plan: {
      event_cursor: 1,
      head: {
        scope: { project_id: 'p', session_id: 's' },
        ref,
        status_revision: 1,
        progress_cursor: 0,
        status: 'active',
        outcome: null,
      },
      version: {
        scope: { project_id: 'p', session_id: 's' },
        ref,
        base: null,
        recorded_at: at,
        reason: 'Initial',
        author: {
          scope: { project_id: 'p', session_id: 's' },
          role: 'main',
          user_id: null,
          run_id: 'run-one',
          alias: 'Researcher',
          resolved_model: null,
          tool_call: null,
          grant: null,
          track_id: null,
        },
        body: {
          goal: 'Study evidence',
          feasibility: 'Available source',
          outputs: [
            { title: 'Report', description: 'A result', evidence_standard: 'Exact evidence' },
          ],
          tracks: [{ track_id: 'track-one', title: 'Analysis' }],
          phases: [
            {
              phase_id: 'phase-one',
              title: 'Validate',
              steps: [
                {
                  step_id: 'step-one',
                  title: 'Inspect',
                  description: 'Read evidence',
                  evidence_standard: 'Known source',
                  track_id: 'track-one',
                },
              ],
            },
          ],
        },
      },
      steps: [
        {
          step_id: 'step-one',
          step_sha256: 'a'.repeat(64),
          matches_current_step: false,
          latest: null,
        },
      ],
    },
  };
}
function next(v: PlanObservation): PlanObservation {
  const n = structuredClone(v);
  n.observed_at = `2026-10-13T04:00:0${n.plan!.head.ref.version + 1}Z`;
  n.plan!.head.ref.version++;
  n.plan!.version.ref.version = n.plan!.head.ref.version;
  n.plan!.version.base = n.plan!.head.ref.version - 1;
  n.plan!.event_cursor++;
  return n;
}
function request(v = observed()): PlanChange {
  return {
    operation_id: 'edit-one',
    base: { ...v.plan!.head.ref },
    body: structuredClone(v.plan!.version.body),
    reason: 'Clarify wording',
  };
}
function ack(r: PlanChange, s: PlanScope = scope): PlanReceipt {
  return {
    scope: { project_id: s.projectId, session_id: s.sessionId },
    operation_id: r.operation_id,
    action: 'revise',
    ref: { plan_id: r.base.plan_id, version: r.base.version + 1 },
    event_sequence: 2,
    status_revision: 1,
    progress_cursor: 0,
  };
}
function response(r: PlanChange, s: PlanScope = scope): Response {
  return new Response(
    JSON.stringify({ project_id: s.projectId, session_id: s.sessionId, receipt: ack(r, s) }),
  );
}
function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
}
let storage: Map<string, string>;
beforeEach(() => {
  storage = new Map();
  vi.stubGlobal('sessionStorage', {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => storage.set(k, v),
    removeItem: (k: string) => storage.delete(k),
  });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
type Transport = (
  scope: PlanScope,
  request: PlanChange,
  signal: AbortSignal,
) => Promise<PlanReceipt>;
function session(
  v = observed(),
  transport: Transport = vi.fn(async (_s, r) => ack(r)),
  refresh = vi.fn(),
) {
  let view!: EditorView;
  const editor = new PlanEditSession(
    scope,
    v,
    true,
    (x) => {
      view = x;
    },
    transport,
    refresh,
  );
  return { editor, transport, refresh, view: () => view };
}
async function reload(plan: string) {
  vi.resetModules();
  return (await import('./planEdits')).loadPlanDraft(scope, plan);
}

describe('structured drafts', () => {
  it('changes only generated text while preserving all reference structure', () => {
    const s = session(),
      original = structuredClone(s.view().draft.body);
    for (const f of textFields(original)) s.editor.edit(f.key, `Revised ${f.label}`);
    expect(sameStructure(original, s.view().draft.body)).toBe(true);
    expect(textFields(s.view().draft.body).every((f) => f.value.startsWith('Revised'))).toBe(true);
    s.editor.edit('track_id', 'foreign');
    expect(s.view().draft.body.phases[0]!.steps[0]!.track_id).toBe('track-one');
    s.editor.stop();
  });
  it('retains unfinished empty text/reason through polling, navigation and storage reload', async () => {
    const v = observed(),
      s = session(v);
    s.editor.edit('goal', '');
    s.editor.reason('');
    const current = next(v);
    current.plan!.version.body.goal = 'New current';
    s.editor.observe(current, true);
    s.editor.stop();
    expect((await reload(v.plan!.head.ref.plan_id)).draft).toMatchObject({
      base: { version: 1 },
      body: { goal: '' },
      reason: '',
    });
  });
  it('retains an independent old baseline across structure change, load-current, save and reload', async () => {
    const v = observed(),
      s = session(v);
    s.editor.edit('goal', '');
    s.editor.reason('Unfinished');
    const changed = next(v);
    changed.plan!.version.body.phases[0]!.steps.push({
      step_id: 'step-two',
      title: 'New step',
      description: 'New work',
      evidence_standard: 'Evidence',
      track_id: null,
    });
    changed.plan!.steps.push({
      step_id: 'step-two',
      step_sha256: 'b'.repeat(64),
      matches_current_step: false,
      latest: null,
    });
    s.editor.observe(changed, true);
    s.editor.adopt();
    expect(s.view().message).toContain('structure changed');
    s.editor.loadCurrent();
    expect(s.view().warning).toBe(false);
    let draft = (await reload(v.plan!.head.ref.plan_id)).draft!;
    expect(draft.body.phases[0]!.steps).toHaveLength(2);
    expect(draft.retained!.body.phases[0]!.steps).toHaveLength(1);
    expect(draft.retained!.body.goal).toBe('');
    s.editor.edit('goal', 'Saved newer wording');
    await s.editor.save();
    const saved = next(changed);
    saved.plan!.version.body = structuredClone(s.view().draft.body);
    s.editor.observe(saved, true);
    s.editor.stop();
    draft = (await reload(v.plan!.head.ref.plan_id)).draft!;
    expect(draft.base.version).toBe(3);
    expect(draft.pending).toBeNull();
    expect(draft.retained!.body.goal).toBe('');
    expect(draft.retained!.baseline.goal).toBe('Study evidence');
  });
  it('initial conflict retains text and requires explicit compatible adoption', async () => {
    const v = observed(),
      transport = vi.fn(async () => {
        throw new PlanWriteError(409, { plan_id: v.plan!.head.ref.plan_id, version: 2 });
      }),
      s = session(v, transport);
    s.editor.edit('goal', 'My goal');
    await s.editor.save();
    expect(s.view().draft.pending).toBeNull();
    const current = next(v);
    current.plan!.version.body.goal = 'Other wording';
    s.editor.observe(current, true);
    await s.editor.save();
    expect(transport).toHaveBeenCalledOnce();
    s.editor.adopt();
    expect(s.view().draft.base.version).toBe(2);
    expect(s.view().draft.body.goal).toBe('My goal');
    s.editor.stop();
  });
  it('treats receipt as history, never overwriting a newer current observation', async () => {
    const v = observed(),
      pending = deferred<PlanReceipt>(),
      transport = vi.fn<Transport>(() => pending.promise),
      s = session(v, transport);
    s.editor.edit('goal', 'Saved goal');
    const save = s.editor.save();
    s.editor.observe(next(next(v)), true);
    pending.resolve(ack(transport.mock.calls[0]![1]));
    await save;
    expect(s.view().current.plan!.head.ref.version).toBe(3);
    expect(s.view().draft.body.goal).toBe('Saved goal');
    expect(s.view().draft.pending!.state).toBe('acknowledged');
    expect(s.refresh).toHaveBeenCalledOnce();
    s.editor.stop();
  });
  it('sends no new request if exact retry cannot be stored, retains memory warning', async () => {
    const v = observed();
    vi.stubGlobal('sessionStorage', {
      setItem: () => {
        throw new Error('Synthetic quota');
      },
      getItem: () => null,
      removeItem: () => undefined,
    });
    const transport = vi.fn<Transport>(),
      s = session(v, transport);
    s.editor.edit('goal', 'Memory only');
    await s.editor.save();
    expect(transport).not.toHaveBeenCalled();
    expect(s.view().draft.pending).toBeNull();
    expect(loadPlanDraft(scope, v.plan!.head.ref.plan_id)).toMatchObject({
      warning: true,
      draft: { body: { goal: 'Memory only' } },
    });
    s.editor.stop();
  });
  it.each(['closed', 'abandoned', 'unobserved'])('keeps %s source read-only', async (kind) => {
    const v = observed();
    if (kind === 'closed') v.session_closed_at = at;
    if (kind === 'abandoned') v.plan!.head.status = 'abandoned';
    const transport = vi.fn<Transport>(),
      s = session(v, transport);
    if (kind === 'unobserved') s.editor.observe(v, false);
    s.editor.edit('goal', 'Forbidden');
    await s.editor.save();
    expect(s.view().draft.body.goal).toBe('Study evidence');
    expect(transport).not.toHaveBeenCalled();
    s.editor.stop();
  });
  it('holds corrupt storage untouched', async () => {
    const v = observed(),
      k = `plan-text-draft:p:s:${v.plan!.head.ref.plan_id}`;
    storage.set(k, '{broken');
    const s = session(v);
    await s.editor.save();
    expect(s.view().blocked).toBe(true);
    expect(storage.get(k)).toBe('{broken');
    s.editor.stop();
  });
  it('allows wording changes while awaiting approval without granting activation', async () => {
    const v = observed();
    v.plan!.head.status = 'awaiting_approval';
    const transport = vi.fn<Transport>(async (_s, r) => ack(r)),
      s = session(v, transport);
    s.editor.edit('goal', 'Qualified wording');
    await s.editor.save();
    expect(transport).toHaveBeenCalledOnce();
    expect(s.view().current.plan!.head.status).toBe('awaiting_approval');
    expect(Object.keys(transport.mock.calls[0]![1])).toEqual([
      'operation_id',
      'base',
      'body',
      'reason',
    ]);
    s.editor.stop();
  });
  it('bounds canonical ASCII bytes and Unicode characters', () => {
    const b = observed().plan!.version.body;
    b.goal = '🔑'.repeat(4096);
    expect(editProblem(b, 'Reason')).toBeNull();
    b.feasibility = b.goal;
    expect(editProblem(b, 'Reason')).toContain('size limit');
    expect(editProblem(b, '')).toContain('reason');
    b.goal = '\uD800';
    expect(editProblem(b, 'Reason')).toContain('text field');
  });
});

describe('uncertain writes', () => {
  it('retries exact old intent and keeps uncertainty after a later conflict and reload', async () => {
    const v = observed(),
      transport = vi.fn<Transport>(async () => {
        throw new PlanWriteError(503);
      }),
      s = session(v, transport);
    s.editor.edit('goal', 'Uncertain');
    await s.editor.save();
    const original = structuredClone(s.view().draft.pending!.request);
    s.editor.edit('goal', 'Changed');
    s.editor.reason('Changed');
    transport.mockImplementation(async () => {
      throw new PlanWriteError(409);
    });
    await s.editor.save();
    expect(transport.mock.calls[1]![1]).toEqual(transport.mock.calls[0]![1]);
    s.editor.stop();
    expect((await reload(original.base.plan_id)).draft!.pending!.request).toEqual(original);
  });
  it('continues explicitly after fresh read, keeps unresolved predecessor through save and refuses second eviction', async () => {
    const v = observed(),
      transport = vi.fn<Transport>(async () => {
        throw new PlanWriteError(503);
      }),
      s = session(v, transport);
    s.editor.edit('goal', 'Earlier');
    await s.editor.save();
    const old = structuredClone(s.view().draft.pending!.request);
    s.editor.continueCurrent();
    expect(s.view().draft.pending).not.toBeNull();
    const current = next(v);
    s.editor.observe(current, true);
    s.editor.continueCurrent();
    expect(s.view().draft.pending).toBeNull();
    expect(s.view().draft.unresolved).toEqual(old);
    s.editor.edit('goal', 'New intent');
    transport.mockImplementation(async (_s, r) => ack(r));
    await s.editor.save();
    const saved = next(current);
    saved.plan!.version.body = structuredClone(s.view().draft.body);
    s.editor.observe(saved, true);
    expect(s.view().draft.unresolved).toEqual(old);
    expect(s.view().draft.retained!.body.goal).toBe('Earlier');
    transport.mockImplementation(async () => {
      throw new PlanWriteError(503);
    });
    await s.editor.save();
    const second = structuredClone(s.view().draft.pending!.request);
    expect(second.operation_id).not.toBe(old.operation_id);
    s.editor.observe(next(saved), true);
    s.editor.continueCurrent();
    expect(s.view().draft.pending!.request).toEqual(second);
    expect(s.view().draft.unresolved).toEqual(old);
    s.editor.stop();
  });
  it('invalidates late unmount/logout responses without erasing pending intent', async () => {
    for (const logout of [false, true]) {
      const v = observed(),
        pending = deferred<PlanReceipt>(),
        s = session(v, () => pending.promise);
      const save = s.editor.save(),
        r = s.view().draft.pending!.request;
      if (logout) s.editor.logout();
      else s.editor.stop();
      pending.resolve(ack(r));
      await save;
      expect(loadPlanDraft(scope, r.base.plan_id).draft!.pending!.state).toBe('unknown');
      expect(s.refresh).not.toHaveBeenCalled();
      s.editor.stop();
    }
  });
});

describe('bounded owned write transport', () => {
  it('posts exact same-origin intent and checks the exact receipt', async () => {
    const r = request(),
      fetch = vi.fn().mockResolvedValue(response(r));
    vi.stubGlobal('fetch', fetch);
    expect(await writePlanChange(scope, r, new AbortController().signal)).toEqual(ack(r));
    expect(fetch.mock.calls[0]).toMatchObject([
      '/api/v0/projects/p/sessions/s/plan/changes',
      {
        method: 'POST',
        redirect: 'error',
        cache: 'no-store',
        body: JSON.stringify(r),
      },
    ]);
  });
  it.each(['scope', 'operation', 'version', 'unsafe'])('refuses wrong %s receipt', async (kind) => {
    const r = request(),
      v = { project_id: 'p', session_id: 's', receipt: ack(r) };
    if (kind === 'scope') v.receipt.scope.session_id = 'other';
    if (kind === 'operation') v.receipt.operation_id = 'other';
    if (kind === 'version') v.receipt.ref.version++;
    if (kind === 'unsafe') v.receipt.event_sequence = 2 ** 53;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(v))));
    await expect(writePlanChange(scope, r, new AbortController().signal)).rejects.toMatchObject({
      status: 0,
    });
  });
  it('owns the session across changed plans and ignored abort, but leaves another session free', async () => {
    vi.useFakeTimers();
    const r = request(),
      pending = deferred<Response>(),
      fetch = vi.fn().mockReturnValueOnce(pending.promise);
    vi.stubGlobal('fetch', fetch);
    const first = writePlanChange(scope, r, new AbortController().signal).catch((e) => e);
    await vi.advanceTimersByTimeAsync(EDIT_DEADLINE_MS);
    const replacement = request();
    await expect(
      writePlanChange(scope, replacement, new AbortController().signal),
    ).rejects.toMatchObject({ status: 0 });
    expect(fetch).toHaveBeenCalledOnce();
    const other = { projectId: 'p', sessionId: 'other' };
    fetch.mockResolvedValue(response(replacement, other));
    await expect(
      writePlanChange(other, replacement, new AbortController().signal),
    ).resolves.toEqual(ack(replacement, other));
    pending.resolve(response(r));
    expect(await first).toBeInstanceOf(PlanWriteError);
    fetch.mockResolvedValue(response(replacement));
    await expect(
      writePlanChange(scope, replacement, new AbortController().signal),
    ).resolves.toEqual(ack(replacement));
  });
  it.each([403, 404, 413, 503])('keeps the status of a %i refusal', async (status) => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ error: { kind: 'x', message: 'private' } }), { status }),
        ),
    );
    await expect(
      writePlanChange(scope, request(), new AbortController().signal),
    ).rejects.toMatchObject({ status, message: 'Plan text save unavailable' });
  });
  it.each(['declared', 'streamed', 'utf8'])('bounds %s response', async (kind) => {
    const stream = new ReadableStream<Uint8Array>({
      pull(c) {
        c.enqueue(
          kind === 'utf8'
            ? new Uint8Array([255])
            : new Uint8Array(kind === 'streamed' ? EDIT_RESPONSE_BYTES + 1 : 0),
        );
        if (kind === 'utf8') c.close();
      },
    });
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(stream, {
          headers: kind === 'declared' ? { 'content-length': String(EDIT_RESPONSE_BYTES + 1) } : {},
        }),
      ),
    );
    await expect(
      writePlanChange(scope, request(), new AbortController().signal),
    ).rejects.toMatchObject({ status: 0 });
  });
  it('freezes caller input and reads only bounded current conflict ref', async () => {
    const r = request(),
      s = { ...scope },
      pending = deferred<Response>();
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(pending.promise));
    const result = writePlanChange(s, r, new AbortController().signal);
    s.sessionId = 'foreign';
    r.base.version = 100;
    pending.resolve(
      new Response(
        JSON.stringify({
          error: {
            current: { plan_id: r.base.plan_id, version: 2 },
            message: 'Ignored synthetic private detail',
          },
        }),
        { status: 409 },
      ),
    );
    await expect(result).rejects.toMatchObject({
      status: 409,
      current: { plan_id: r.base.plan_id, version: 2 },
      message: 'Plan text save unavailable',
    });
  });
});

describe('plain structured projection', () => {
  it('escapes plain text and never reads storage or network during SSR', () => {
    const v = observed();
    v.plan!.version.body.goal = '<script>synthetic</script>';
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    vi.stubGlobal('sessionStorage', {
      getItem: () => {
        throw new Error('SSR forbidden');
      },
    });
    const html = render(PlanEditor, {
      props: {
        projectId: 'p',
        sessionId: 's',
        observation: v,
        observed: true,
        refresh: () => undefined,
      },
    }).body;
    expect(html).toContain('Phase 1, step 1 description');
    expect(html).not.toContain('<script>synthetic');
    expect(html).toContain('&lt;script');
    expect(html).toContain('<fieldset disabled');
    expect(fetch).not.toHaveBeenCalled();
  });
  it('mounts in the real plan panel, shows escaped version reason, and keeps closed/no-plan read-only', () => {
    const v = observed();
    v.session_closed_at = at;
    v.plan!.version.reason = 'USER reason <script>synthetic</script>';
    const html = render(PlanPanel, {
      props: {
        projectId: 'p',
        sessionId: 's',
        initialState: { phase: 'observed', data: v, busy: false },
      },
    }).body;
    expect(html).toContain('Edit plan text');
    expect(html).toContain('This recorded plan is read-only');
    expect(html).toContain('Reason for this version: USER reason &lt;script');
    expect(html).not.toContain('<script>synthetic');
    v.plan = null;
    expect(
      render(PlanPanel, {
        props: {
          projectId: 'p',
          sessionId: 's',
          initialState: { phase: 'observed', data: v, busy: false },
        },
      }).body,
    ).not.toContain('Edit plan text');
  });
  it('does not mount an editor or display prior data under a changed scope', () => {
    const v = observed();
    const html = render(PlanPanel, {
      props: {
        projectId: 'other',
        sessionId: 's',
        initialState: { phase: 'observed', data: v, busy: false },
      },
    }).body;
    expect(html).not.toContain('Study evidence');
    expect(html).not.toContain('Edit plan text');
  });
  it('sticky logout invalidates delayed results and prevents new editor writes', async () => {
    vi.resetModules();
    const m = await import('./planEdits'),
      session = await import('./session'),
      r = request(),
      pending = deferred<Response>();
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(pending.promise));
    const first = m.writePlanChange(scope, r, new AbortController().signal).catch((e) => e);
    session.signOut();
    pending.resolve(response(r));
    expect(await first).toBeInstanceOf(m.PlanWriteError);
    await expect(
      m.writePlanChange(scope, request(), new AbortController().signal),
    ).rejects.toMatchObject({ status: 401 });
    const transport = vi.fn<Transport>(),
      editor = new m.PlanEditSession(scope, observed(), true, () => undefined, transport);
    await editor.save();
    expect(transport).not.toHaveBeenCalled();
    editor.stop();
  });
});
