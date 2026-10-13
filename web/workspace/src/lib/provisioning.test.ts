// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Project } from './api';
import EnvironmentPanel from '../views/EnvironmentPanel.svelte';
import { environmentChoices, provisionData, provisionPath, readProvision, ProvisionReadError, ProvisionReader,
  PROVISION_DEADLINE_MS, type ProvisionObservation, type OperationSummary, type OperationDetail,
  type FilePage, type HistoryPage, type ProbePage, type ProvisionData, type ReadState } from './provisioning';
const scope = {projectId: 'p', environmentId: 'e'}, time = '2026-10-13T06:00:00Z';
const common = {project_id: 'p', environment_id: 'e', observed_at: time};
function project(): Project {return {project_id: 'p', profile: 'minimal', policy: 'sha256:' + 'a'.repeat(64), created_at: time, updated_at: time, default_environment_id: 'e', sessions: [{session_id: 's', environment_id: 'e', dedicated: false, opened_at: time, closed_at: null, runs: 0}], environments: [{environment_id: 'e', default: true, sessions: ['s']}]};}
function op(): OperationSummary {return {provision_id: 'prv-one', operation_id: 'op-one', session_id: 's', seq: 3, state: 'awaiting_permission', outcome: 'awaiting_permission', reason: null, stage: 'permission', planned_revision: 0, revision_before: 0, revision_after: null, permission_request: 7, submitted_at: time, started_at: time, ended_at: null, files_total: 2, changes_total: 0, probes_total: 0, executions_total: 1, notes_total: 0};}
function overview(): ProvisionObservation {return {...common, gate: {generation: 1, mode: 'ready', operation: null, revision: 0, recorded: true}, current_revision: 0, queue_total: 1, operations: [op()]};}
function detail(): OperationDetail {return {...common, summary: op(), request: {operation_id: 'op-one', context: {project_id: 'p', session_id: 's', environment_id: 'e', policy_revision: null, actor: 'agent'}, purpose: '<img src=x onerror=attack()>', capability: null, conda: ['networkx'], pip: [], pip_reason: null, system: []}, policy: null, catalog: null, next_step: 'Ask the operator', error_present: false, error_kind: null};}
function files(): FilePage {return {...common, provision_id: 'prv-one', seq: 3, total: 2, offset: 0, limit: 8, next_offset: null, files: [{ecosystem: 'conda', name: 'example', version: '1', build: null, channel: 'conda-forge', subdir: 'linux-64', file: 'example-1.conda', url: null, sha256: null, license: null, agpl: false, action: 'escalate', rule: 'packages.conda'}, {ecosystem: 'pypi', name: 'other', version: '2', build: null, channel: null, subdir: null, file: 'other.whl', url: 'https://example.invalid/<script>', sha256: null, license: 'AGPL-3.0', agpl: true, action: 'deny', rule: null}]};}
function history(): HistoryPage {return {...common, retained_total: 2, first_retained: 0, target: 1, after: -1, limit: 8, next_after: null, revisions: [{revision: 1, record_sha256: 'b'.repeat(64), fingerprint: 'c'.repeat(32), managed: false, changed_by: null, operation: null, recorded_at: time, lockfile: null, commands_total: 0, omitted_commands: 0, probes_total: 1}]};}
function probes(): ProbePage {return {...common, revision: 1, record_sha256: 'b'.repeat(64), total: 1, offset: 0, limit: 8, next_offset: null, probes: [{capability: null, probe: 'installed:example', version: 1, kind: 'import_check', status: 'passed', revision: null, execution_id: null, output: '<script>attack()</script>', duration_seconds: 0, operation: null, recorded_at: time}]};}
function deferred<T>() {let resolve!: (v: T) => void; const promise = new Promise<T>(yes => {resolve = yes;}); return {promise, resolve};}
const owners: ProvisionReader[] = [];
function reader(read: (signal: AbortSignal) => Promise<ProvisionData>, poll = true) {const states: ReadState[] = []; const owner = new ProvisionReader(read, v => states.push(v), poll); owners.push(owner); return {owner, states};}
afterEach(() => {for (const owner of owners.splice(0)) owner.stop(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks();});

describe('canonical read scopes and pinned DTOs', () => {
  it('retains recorded dedicated closed environments without reopen authority', () => {const p = project(); p.sessions.push({session_id: 'closed', environment_id: 'd', dedicated: true, opened_at: time, closed_at: time, runs: 0}); p.environments.push({environment_id: 'd', default: false, sessions: ['closed']}); expect(environmentChoices('p', p)).toHaveLength(2); expect(environmentChoices('foreign', p)).toEqual([]);});
  it.each(['duplicate', 'missing session', 'false default', 'unsafe', 'foreign mapping'])('refuses %s project mappings', mode => {const p = project(); if (mode === 'duplicate') p.environments.push(p.environments[0]!); if (mode === 'missing session') p.environments[0]!.sessions = []; if (mode === 'false default') p.environments[0]!.default = false; if (mode === 'unsafe') p.environments[0]!.environment_id = '../other'; if (mode === 'foreign mapping') p.sessions[0]!.environment_id = 'other'; expect(environmentChoices('p', p)).toEqual([]);});
  it('keeps absent gate/unknown revision/empty queue explicit', () => {const v = {...overview(), gate: {generation: 0, mode: 'ready', operation: null, revision: null, recorded: false}, current_revision: null, queue_total: 0, operations: []}; expect(provisionData(v, scope, {kind: 'summary'})).toEqual(v);});
  it.each([
    ['project', (v: ProvisionObservation) => {v.project_id = 'q';}], ['environment', (v: ProvisionObservation) => {v.environment_id = 'f';}],
    ['calendar', (v: ProvisionObservation) => {v.observed_at = '2026-02-31T00:00:00Z';}], ['naive time', (v: ProvisionObservation) => {v.observed_at = '2026-10-13T00:00:00';}],
    ['rounded revision', (v: ProvisionObservation) => {v.current_revision = 2 ** 53;}], ['wrong total', (v: ProvisionObservation) => {v.queue_total = 0;}],
    ['duplicate', (v: ProvisionObservation) => {v.operations.push(op()); v.queue_total++;}], ['unknown state', (v: ProvisionObservation) => {v.operations[0]!.state = 'invented' as OperationSummary['state'];}],
    ['large files count', (v: ProvisionObservation) => {v.operations[0]!.files_total = 2001;}], ['false default', (v: ProvisionObservation) => {v.gate.recorded = false;}],
  ])('refuses %s summary', (_name, change) => {const v = overview(); change(v); expect(() => provisionData(v, scope, {kind: 'summary'})).toThrow(ProvisionReadError);});
  it('binds detail scalars and request scope independent of JSON key order', () => {const d = detail(); d.summary = Object.fromEntries(Object.entries(d.summary).reverse()) as unknown as OperationSummary; expect(provisionData(d, scope, {kind: 'operation', operation: op()})).toEqual(d); for (const change of [{seq: 4}, {session_id: 'foreign'}, {files_total: 1}]) expect(() => provisionData({...d, summary: {...d.summary, ...change}}, scope, {kind: 'operation', operation: op()})).toThrow(); expect(() => provisionData({...detail(), request: {...detail().request, context: {...detail().request.context, environment_id: 'foreign'}}}, scope, {kind: 'operation', operation: op()})).toThrow(); expect(() => provisionData({...detail(), error_kind: 'internal'}, scope, {kind: 'operation', operation: op()})).toThrow();});
  it('binds exact file totals/subset offsets/seq/continuation', () => {expect(provisionData(files(), scope, {kind: 'files', operation: op(), offset: 0})).toEqual(files()); for (const v of [{...files(), seq: 4}, {...files(), total: 3}, {...files(), next_offset: 2}, {...files(), offset: 1}, {...files(), files: []}]) expect(() => provisionData(v, scope, {kind: 'files', operation: op(), offset: 0})).toThrow(); expect(provisionData({...files(), total: 0, files: []}, scope, {kind: 'files', operation: {...op(), files_total: 0}, offset: 0})).toMatchObject({total: 0, files: []});});
  it('pins history and probe record while preserving unknown revision attribution', () => {expect(provisionData(history(), scope, {kind: 'history', after: -1, target: null})).toEqual(history()); expect(() => provisionData({...history(), target: 2}, scope, {kind: 'history', after: -1, target: 1})).toThrow(); expect(() => provisionData({...history(), revisions: [history().revisions[0]!, history().revisions[0]!]}, scope, {kind: 'history', after: -1, target: null})).toThrow(); expect(() => provisionData({...history(), next_after: 1}, scope, {kind: 'history', after: -1, target: null})).toThrow(); const selected = {kind: 'probes' as const, revision: history().revisions[0]!, offset: 0}; expect(provisionData(probes(), scope, selected)).toEqual(probes()); expect(() => provisionData({...probes(), record_sha256: 'f'.repeat(64)}, scope, selected)).toThrow(); expect(() => provisionData({...probes(), probes: [{...probes().probes[0], revision: 0}]}, scope, selected)).toThrow();});
  it('refuses extra peer fields and oversized Unicode prose', () => {expect(() => provisionData({...overview(), token: 'synthetic'}, scope, {kind: 'summary'})).toThrow(); expect(() => provisionData({...detail(), raw_error: 'synthetic'}, scope, {kind: 'operation', operation: op()})).toThrow(); expect(() => provisionData({...detail(), request: {...detail().request, purpose: '😀'.repeat(2001)}}, scope, {kind: 'operation', operation: op()})).toThrow();});
  it('builds only finite read selectors with exact pins', () => {expect(provisionPath(scope, {kind: 'summary'})).toBe('/api/v0/projects/p/environments/e/provisioning/observations'); expect(provisionPath(scope, {kind: 'files', operation: op(), offset: 0})).toContain('/operations/prv-one/files?seq=3&offset=0&limit=8'); expect(provisionPath(scope, {kind: 'history', after: 0, target: 1})).toContain('/revisions?after=0&limit=8&target=1'); expect(() => provisionPath({projectId: '../q', environmentId: 'e'}, {kind: 'summary'})).toThrow(); expect(() => provisionPath(scope, {kind: 'files', operation: op(), offset: 3})).toThrow();});
});

describe('bounded actual fetch/body ownership', () => {
  it('uses same-origin GET/no-store/no-redirect with no action body', async () => {const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(overview()))); vi.stubGlobal('fetch', fetch); expect(await readProvision(scope, {kind: 'summary'}, new AbortController().signal)).toEqual(overview()); expect(fetch.mock.calls[0]![1]).toMatchObject({cache: 'no-store', redirect: 'error', headers: {Accept: 'application/json'}}); expect(fetch.mock.calls[0]![1]).not.toHaveProperty('body');});
  it.each([400, 401, 404, 409, 422, 503])('cancels %i peer errors without retaining raw text', async status => {const cancel = vi.fn(); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({cancel}), {status}))); await expect(readProvision(scope, {kind: 'summary'}, new AbortController().signal)).rejects.toMatchObject({status, message: 'Environment observation unavailable'}); expect(cancel).toHaveBeenCalledOnce();});
  it('rejects malformed UTF8/JSON, foreign scope, redirect and byte/chunk overflow', async () => {for (const response of [new Response(new Uint8Array([255])), new Response('invalid'), new Response(JSON.stringify({...overview(), environment_id: 'foreign'})), new Response('{}', {headers: {'content-length': '65537'}})]) {vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response)); await expect(readProvision(scope, {kind: 'summary'}, new AbortController().signal)).rejects.toBeInstanceOf(ProvisionReadError);} for (const large of [true, false]) {const cancel = vi.fn(); const stream = new ReadableStream<Uint8Array>({pull(c) {c.enqueue(new Uint8Array(large ? 65537 : 0));}, cancel}); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream))); await expect(readProvision(scope, {kind: 'summary'}, new AbortController().signal)).rejects.toBeInstanceOf(ProvisionReadError); expect(cancel).toHaveBeenCalledOnce();} const response = new Response('{}'); Object.defineProperty(response, 'redirected', {value: true}); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response)); await expect(readProvision(scope, {kind: 'summary'}, new AbortController().signal)).rejects.toBeInstanceOf(ProvisionReadError);});
  it('retains the global owner through ignored header abort across scope changes', async () => {vi.useFakeTimers(); const pending = deferred<Response>(); const fetch = vi.fn().mockReturnValue(pending.promise); vi.stubGlobal('fetch', fetch); const first = readProvision(scope, {kind: 'summary'}, new AbortController().signal).catch(e => e); await vi.advanceTimersByTimeAsync(PROVISION_DEADLINE_MS); await expect(readProvision({projectId: 'q', environmentId: 'f'}, {kind: 'summary'}, new AbortController().signal)).rejects.toBeInstanceOf(ProvisionReadError); expect(fetch).toHaveBeenCalledOnce(); pending.resolve(new Response(JSON.stringify(overview()))); expect(await first).toBeInstanceOf(ProvisionReadError); fetch.mockResolvedValue(new Response(JSON.stringify(files()))); expect(await readProvision(scope, {kind: 'files', operation: op(), offset: 0}, new AbortController().signal)).toEqual(files());});
  it('bounds body reads by the same timeout and drains cancel', async () => {vi.useFakeTimers(); const cancel = vi.fn(); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({cancel})))); const pending = readProvision(scope, {kind: 'summary'}, new AbortController().signal).catch(e => e); await vi.advanceTimersByTimeAsync(PROVISION_DEADLINE_MS); expect(await pending).toBeInstanceOf(ProvisionReadError); expect(cancel).toHaveBeenCalledOnce();});
});

describe('mounted visible sequential observation lifecycle', () => {
  it('waits for an ignored abort and discards late timeout data before resuming', async () => {vi.useFakeTimers(); const pending = deferred<ProvisionData>(); const read = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue(overview()); const {owner, states} = reader(read); owner.setVisible(true); await vi.advanceTimersByTimeAsync(PROVISION_DEADLINE_MS); expect(states.at(-1)).toMatchObject({phase: 'unavailable', busy: true}); owner.setVisible(false); owner.setVisible(true); await vi.advanceTimersByTimeAsync(30_000); expect(read).toHaveBeenCalledOnce(); pending.resolve(overview()); await vi.advanceTimersByTimeAsync(0); expect(read).toHaveBeenCalledTimes(2); expect(states.at(-1)).toMatchObject({phase: 'observed', busy: false});});
  it('retains dated previous facts under503, stops404 and invalidates409 selection', async () => {vi.useFakeTimers(); const read = vi.fn().mockResolvedValueOnce(overview()).mockRejectedValue(new ProvisionReadError(503)); const {owner, states} = reader(read); owner.setVisible(true); await vi.advanceTimersByTimeAsync(0); await vi.advanceTimersByTimeAsync(3000); expect(states.at(-1)).toMatchObject({phase: 'unavailable', data: overview()}); owner.stop(); const changed = reader(async () => {throw new ProvisionReadError(409);}, false); changed.owner.setVisible(true); await vi.advanceTimersByTimeAsync(0); expect(changed.states.at(-1)?.phase).toBe('changed'); const missing = vi.fn().mockRejectedValue(new ProvisionReadError(404)); const gone = reader(missing); gone.owner.setVisible(true); await vi.advanceTimersByTimeAsync(30_000); expect(missing).toHaveBeenCalledOnce(); expect(gone.states.at(-1)?.data).toBeNull();});
  it('hide/unmount refuse late publication and selected detail does not repeat failures', async () => {vi.useFakeTimers(); const pending = deferred<ProvisionData>(); const read = vi.fn().mockReturnValue(pending.promise); const {owner, states} = reader(read); owner.setVisible(true); owner.setVisible(false); pending.resolve(overview()); await vi.advanceTimersByTimeAsync(30_000); expect(states.at(-1)?.phase).toBe('paused'); expect(read).toHaveBeenCalledOnce(); owner.stop(); const onceRead = vi.fn().mockRejectedValue(new ProvisionReadError(503)); const once = reader(onceRead, false); once.owner.setVisible(true); await vi.advanceTimersByTimeAsync(30_000); expect(onceRead).toHaveBeenCalledOnce();});
  it('sign-out clears data and cannot resume after a late result', async () => {vi.useFakeTimers(); const read = vi.fn().mockResolvedValue(overview()); const {owner, states} = reader(read); owner.setVisible(true); await vi.advanceTimersByTimeAsync(0); owner.logout(); owner.setVisible(true); await vi.advanceTimersByTimeAsync(30_000); expect(states.at(-1)).toMatchObject({phase: 'signed_out', data: null}); expect(read).toHaveBeenCalledOnce();});
});

describe('escaped read-only view', () => {
  it('shows project-wide queue, independent VM/package state and dated previous view', () => {const html = render(EnvironmentPanel, {props: {projectId: 'p', project: project(), initialView: {phase: 'unavailable', data: overview(), busy: false}}}).body; expect(html).toContain('Previous observation'); expect(html).toContain('VM state'); expect(html).toContain('not observed'); expect(html).toContain('Permission request 7'); expect(html).toContain('whole project environment'); expect(html).not.toMatch(/<button[^>]*>\s*(Approve|Deny|Install|Cancel|Rebuild)/);});
  it('shows exact file count, unknown declared licenses and AGPL without HTML activation', () => {const html = render(EnvironmentPanel, {props: {projectId: 'p', project: project(), initialView: {phase: 'observed', data: overview(), busy: false}, initialSelection: {kind: 'files', operation: op(), offset: 0}, initialDetail: {phase: 'observed', data: files(), busy: false}}}).body; expect(html).toContain('Showing files 1–2 of 2'); expect(html).toContain('unknown'); expect(html).toContain('AGPL flagged'); expect(html).not.toContain('<script>'); expect(html).toContain('&lt;script>');});
  it('keeps probe attribution and import-check limitations literal', () => {const html = render(EnvironmentPanel, {props: {projectId: 'p', project: project(), initialSelection: {kind: 'probes', revision: history().revisions[0]!, offset: 0}, initialDetail: {phase: 'observed', data: probes(), busy: false}}}).body; expect(html).toContain('Revision attribution unknown'); expect(html).toContain('does not prove functional capability'); expect(html).toContain('&lt;script>'); expect(html).not.toContain('<script>');});
});

describe('complete pagination and lifecycle boundaries', () => {
  it('keeps a nine-file set distinct from its first eight-file page', () => {
    const operation = {...op(), files_total: 9};
    const first = {...files(), total: 9, next_offset: 8, files: Array.from({length: 8}, (_, i) => ({...files().files[0]!, name: `package${i}`}))};
    const last = {...files(), total: 9, offset: 8, files: [{...files().files[0]!, name: 'package8'}]};
    expect(provisionData(first, scope, {kind: 'files', operation, offset: 0})).toEqual(first);
    expect(provisionData(last, scope, {kind: 'files', operation, offset: 8})).toEqual(last);
    expect(() => provisionData({...first, next_offset: null}, scope, {kind: 'files', operation, offset: 0})).toThrow();
    const html = render(EnvironmentPanel, {props: {projectId: 'p', project: project(), initialView: {phase: 'observed', data: {...overview(), operations: [operation]}, busy: false}, initialSelection: {kind: 'files', operation, offset: 0}, initialDetail: {phase: 'observed', data: first, busy: false}}}).body;
    expect(html).toContain('Showing files 1–8 of 9'); expect(html).toContain('This is a page of the file set');
    expect(html).not.toContain('package8');
  });
  it('keeps a pinned retained history window and a nine-probe page complete', () => {
    const rows = Array.from({length: 8}, (_, revision) => ({...history().revisions[0]!, revision}));
    const first = {...history(), retained_total: 9, target: 8, next_after: 7, revisions: rows};
    expect(provisionData(first, scope, {kind: 'history', after: -1, target: null})).toEqual(first);
    const last = {...first, after: 7, next_after: null, revisions: [{...rows[0]!, revision: 8}]};
    expect(provisionData(last, scope, {kind: 'history', after: 7, target: 8})).toEqual(last);
    for (const changed of [{...first, first_retained: 9}, {...first, retained_total: 1}, {...first, first_retained: 1}, {...first, retained_total: 0}]) expect(() => provisionData(changed, scope, {kind: 'history', after: -1, target: null})).toThrow();
    const revision = {...history().revisions[0]!, probes_total: 9};
    const probeFirst = {...probes(), total: 9, next_offset: 8, probes: Array.from({length: 8}, (_, i) => ({...probes().probes[0]!, probe: `check-${i}`}))};
    expect(provisionData(probeFirst, scope, {kind: 'probes', revision, offset: 0})).toEqual(probeFirst);
    expect(provisionData({...probes(), total: 9, offset: 8}, scope, {kind: 'probes', revision, offset: 8})).toMatchObject({offset: 8, total: 9});
  });
  it('refuses contradictory gates, finished queue entries and request kinds', () => {
    for (const gate of [{...overview().gate, mode: 'ready', operation: 'op'}, {...overview().gate, mode: 'needs_recovery', operation: null}]) expect(() => provisionData({...overview(), gate}, scope, {kind: 'summary'})).toThrow();
    expect(() => provisionData({...overview(), operations: [{...op(), state: 'finished'}]}, scope, {kind: 'summary'})).toThrow();
    for (const request of [{...detail().request, capability: 'gp'}, {...detail().request, conda: []}, {...detail().request, pip_reason: 'reason'}, {...detail().request, pip: ['networkx'], pip_reason: 'reason'}, {...detail().request, purpose: 'bad\u0000text'}, {...detail().request, conda: [], capability: 'bad/capability'}]) expect(() => provisionData({...detail(), request}, scope, {kind: 'operation', operation: op()})).toThrow();
    expect(() => provisionData({...detail(), error_present: true, error_kind: null}, scope, {kind: 'operation', operation: op()})).toThrow();
  });
  it('retains actual body-cancel ownership even when cancellation ignores the deadline', async () => {
    vi.useFakeTimers(); const drained = deferred<void>(); const cancel = vi.fn().mockReturnValue(drained.promise);
    const fetch = vi.fn().mockResolvedValueOnce(new Response(new ReadableStream({cancel}))).mockResolvedValue(new Response(JSON.stringify(overview())));
    vi.stubGlobal('fetch', fetch);
    const first = readProvision(scope, {kind: 'summary'}, new AbortController().signal).catch(e => e);
    await vi.advanceTimersByTimeAsync(PROVISION_DEADLINE_MS); expect(cancel).toHaveBeenCalledOnce();
    await expect(readProvision(scope, {kind: 'summary'}, new AbortController().signal)).rejects.toBeInstanceOf(ProvisionReadError); expect(fetch).toHaveBeenCalledOnce();
    drained.resolve(); expect(await first).toBeInstanceOf(ProvisionReadError);
    expect(await readProvision(scope, {kind: 'summary'}, new AbortController().signal)).toEqual(overview());
  });
  it.each([401, 404, 409])('settles terminal %i without leaving visible controls busy', async status => {
    vi.useFakeTimers(); const read = vi.fn().mockRejectedValue(new ProvisionReadError(status)); const {owner, states} = reader(read);
    owner.setVisible(true); await vi.advanceTimersByTimeAsync(0);
    expect(states.at(-1)?.busy).toBe(false); owner.setVisible(false); owner.setVisible(true); await vi.advanceTimersByTimeAsync(30_000); expect(read).toHaveBeenCalledOnce();
  });
  it('marks a selected departed operation historical and hides foreign selected facts', () => {
    const props = {projectId: 'p', project: project(), initialView: {phase: 'observed' as const, data: {...overview(), queue_total: 0, operations: []}, busy: false}, initialSelection: {kind: 'operation' as const, operation: op()}, initialDetail: {phase: 'observed' as const, data: detail(), busy: false}};
    expect(render(EnvironmentPanel, {props}).body).toContain('This selected record is historical');
    expect(render(EnvironmentPanel, {props: {...props, initialDetail: {...props.initialDetail, data: {...detail(), environment_id: 'foreign'}}}}).body).not.toContain('Ask the operator');
  });
  it('initiation of logout blocks new or late transport owners', async () => {
    vi.resetModules(); const transport = await import('./provisioning'), auth = await import('./kernels');
    const pending = deferred<Response>(); const fetch = vi.fn().mockReturnValue(pending.promise); vi.stubGlobal('fetch', fetch);
    const read = transport.readProvision(scope, {kind: 'summary'}, new AbortController().signal).catch(e => e);
    auth.stopKernelReadsForLogout(); pending.resolve(new Response(JSON.stringify(overview())));
    expect(await read).toBeInstanceOf(transport.ProvisionReadError);
    await expect(transport.readProvision(scope, {kind: 'summary'}, new AbortController().signal)).rejects.toMatchObject({status: 401});
    expect(fetch).toHaveBeenCalledOnce(); vi.resetModules();
  });
});

describe('explicit current retained operations', () => {
  it('loads a finished current record by exact selected ID without asserting revision-era seq', () => {
    const current = {...detail(), summary: {...op(), seq: 9, state: 'finished' as const, outcome: 'completed', ended_at: time}};
    const selected = {kind: 'retained_operation' as const, provisionId: 'prv-one'};
    expect(provisionPath(scope, selected)).toBe('/api/v0/projects/p/environments/e/provisioning/observations/operations/prv-one');
    expect(provisionData(current, scope, selected)).toEqual(current);
    expect(() => provisionData({...current, summary: {...current.summary, provision_id: 'other'}}, scope, selected)).toThrow();
    expect(() => provisionData({...current, request: {...current.request, operation_id: 'other'}}, scope, selected)).toThrow();
    expect(() => provisionPath(scope, {...selected, provisionId: '../foreign'})).toThrow();
    expect(() => provisionData(current, scope, {kind: 'operation', operation: op()})).toThrow();
  });
  it('pins the returned current sequence for files and refuses subsequent drift', async () => {
    const current = {...detail(), summary: {...op(), seq: 9}};
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(current))).mockResolvedValueOnce(new Response(JSON.stringify({...files(), seq: 9})));
    vi.stubGlobal('fetch', fetch);
    const read = await readProvision(scope, {kind: 'retained_operation', provisionId: 'prv-one'}, new AbortController().signal) as OperationDetail;
    expect(await readProvision(scope, {kind: 'files', operation: read.summary, offset: 0, retained: true}, new AbortController().signal)).toMatchObject({seq: 9});
    expect(fetch.mock.calls[1]![0]).toContain('/files?seq=9&offset=0&limit=8');
    expect(() => provisionData(files(), scope, {kind: 'files', operation: read.summary, offset: 0, retained: true})).toThrow();
  });
  it('labels retained operation state and leaves its file pagination available outside the queue', () => {
    const empty = {...overview(), queue_total: 0, operations: []};
    const props = {projectId: 'p', project: project(), initialView: {phase: 'observed' as const, data: empty, busy: false}, initialSelection: {kind: 'retained_operation' as const, provisionId: 'prv-one'}, initialDetail: {phase: 'observed' as const, data: detail(), busy: false}};
    const html = render(EnvironmentPanel, {props}).body;
    expect(html).toContain('Current retained operation state'); expect(html).toContain('does not reproduce its state at that revision');
    expect(html).not.toContain('This selected record is historical');
    const fileHtml = render(EnvironmentPanel, {props: {...props, initialSelection: {kind: 'files', operation: op(), offset: 0, retained: true}, initialDetail: {...props.initialDetail, data: files()}}}).body;
    expect(fileHtml).toContain('explicitly loaded current retained operation'); expect(fileHtml).not.toContain('This selected record is historical');
    const historyHtml = render(EnvironmentPanel, {props: {...props, initialSelection: {kind: 'history', after: -1, target: null}, initialDetail: {...props.initialDetail, data: {...history(), revisions: [{...history().revisions[0]!, operation: 'prv-one'}]}}}}).body;
    expect(historyHtml).toContain('Load current retained operation');
  });
});

describe('recorded permission references', () => {
  it('does not infer an unanswered permission from a retained reference during installation', () => {
    const data = {...overview(), operations: [{...op(), state: 'installing' as const, stage: 'link', outcome: null}]};
    const html = render(EnvironmentPanel, {props: {projectId: 'p', project: project(), initialView: {phase: 'observed', data, busy: false}}}).body;
    expect(html).toContain('recorded reference, current decision not shown'); expect(html).not.toContain('waiting for an operator answer');
  });
});
