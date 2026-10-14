// SPDX-License-Identifier: MIT
// Rendered HTML is compared with each whitespace run as one space, so that line breaks in
// the components' source do not matter.
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Project } from './api';
import EnvironmentPanel from '../views/EnvironmentPanel.svelte';
import { environmentChoices, provisionData, provisionPath, readProvision, ProvisionReadError, ProvisionReader,
  PROVISION_DEADLINE_MS, type ProvisionObservation, type OperationSummary, type OperationDetail,
  DIFF_DEADLINE_MS, PACKAGES_DEADLINE_MS, diffIdentity, differenceKey, packageIdentity, packageKey, type RevisionPackagePage, type LockfileDiffPage, type CondaEntry, type LockfileDifference, type Selection,
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
  it('shows project-wide queue, independent VM/package state and dated previous view', () => {const html = render(EnvironmentPanel, {props: {projectId: 'p', project: project(), initialView: {phase: 'unavailable', data: overview(), busy: false}}}).body.replace(/\s+/g, ' '); expect(html).toContain('Previous observation'); expect(html).toContain('VM state'); expect(html).toContain('not observed'); expect(html).toContain('Permission request 7'); expect(html).toContain('whole project environment'); expect(html).not.toMatch(/<button[^>]*>\s*(Approve|Deny|Install|Cancel|Rebuild)/);});
  it('shows exact file count, unknown declared licenses and AGPL without HTML activation', () => {const html = render(EnvironmentPanel, {props: {projectId: 'p', project: project(), initialView: {phase: 'observed', data: overview(), busy: false}, initialSelection: {kind: 'files', operation: op(), offset: 0}, initialDetail: {phase: 'observed', data: files(), busy: false}}}).body.replace(/\s+/g, ' '); expect(html).toContain('Showing files 1–2 of 2'); expect(html).toContain('unknown'); expect(html).toContain('AGPL flagged'); expect(html).not.toContain('<script>'); expect(html).toContain('&lt;script>');});
  it('keeps probe attribution and import-check limitations literal', () => {const html = render(EnvironmentPanel, {props: {projectId: 'p', project: project(), initialSelection: {kind: 'probes', revision: history().revisions[0]!, offset: 0}, initialDetail: {phase: 'observed', data: probes(), busy: false}}}).body.replace(/\s+/g, ' '); expect(html).toContain('Revision attribution unknown'); expect(html).toContain('does not prove functional capability'); expect(html).toContain('&lt;script>'); expect(html).not.toContain('<script>');});
});

describe('complete pagination and lifecycle boundaries', () => {
  it('keeps a nine-file set distinct from its first eight-file page', () => {
    const operation = {...op(), files_total: 9};
    const first = {...files(), total: 9, next_offset: 8, files: Array.from({length: 8}, (_, i) => ({...files().files[0]!, name: `package${i}`}))};
    const last = {...files(), total: 9, offset: 8, files: [{...files().files[0]!, name: 'package8'}]};
    expect(provisionData(first, scope, {kind: 'files', operation, offset: 0})).toEqual(first);
    expect(provisionData(last, scope, {kind: 'files', operation, offset: 8})).toEqual(last);
    expect(() => provisionData({...first, next_offset: null}, scope, {kind: 'files', operation, offset: 0})).toThrow();
    const html = render(EnvironmentPanel, {props: {projectId: 'p', project: project(), initialView: {phase: 'observed', data: {...overview(), operations: [operation]}, busy: false}, initialSelection: {kind: 'files', operation, offset: 0}, initialDetail: {phase: 'observed', data: first, busy: false}}}).body.replace(/\s+/g, ' ');
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
    expect(render(EnvironmentPanel, {props}).body.replace(/\s+/g, ' ')).toContain('This selected record is historical');
    expect(render(EnvironmentPanel, {props: {...props, initialDetail: {...props.initialDetail, data: {...detail(), environment_id: 'foreign'}}}}).body.replace(/\s+/g, ' ')).not.toContain('Ask the operator');
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
    const html = render(EnvironmentPanel, {props}).body.replace(/\s+/g, ' ');
    expect(html).toContain('Current retained operation state'); expect(html).toContain('does not reproduce its state at that revision');
    expect(html).not.toContain('This selected record is historical');
    const fileHtml = render(EnvironmentPanel, {props: {...props, initialSelection: {kind: 'files', operation: op(), offset: 0, retained: true}, initialDetail: {...props.initialDetail, data: files()}}}).body.replace(/\s+/g, ' ');
    expect(fileHtml).toContain('explicitly loaded current retained operation'); expect(fileHtml).not.toContain('This selected record is historical');
    const historyHtml = render(EnvironmentPanel, {props: {...props, initialSelection: {kind: 'history', after: -1, target: null}, initialDetail: {...props.initialDetail, data: {...history(), revisions: [{...history().revisions[0]!, operation: 'prv-one'}]}}}}).body.replace(/\s+/g, ' ');
    expect(historyHtml).toContain('Load current retained operation');
  });
});

describe('recorded permission references', () => {
  it('does not infer an unanswered permission from a retained reference during installation', () => {
    const data = {...overview(), operations: [{...op(), state: 'installing' as const, stage: 'link', outcome: null}]};
    const html = render(EnvironmentPanel, {props: {projectId: 'p', project: project(), initialView: {phase: 'observed', data, busy: false}}}).body.replace(/\s+/g, ' ');
    expect(html).toContain('recorded reference, current decision not shown'); expect(html).not.toContain('waiting for an operator answer');
  });
});

function diffSelection(): Extract<Selection, {kind: 'diff'}> {
  const row = history().revisions[0]!;
  return {kind: 'diff', offset: 0, before: {...row, revision: 0, record_sha256: 'a'.repeat(64), lockfile: {sha256: 'd'.repeat(64), size: 128, conda_packages: 1, pip_distributions: 0}}, after: {...row, lockfile: {sha256: 'e'.repeat(64), size: 129, conda_packages: 1, pip_distributions: 0}}};
}
function diffEntry(): CondaEntry {return {name: 'package', version: '1', build: '0', channel: 'conda-forge', subdir: 'linux-64', file: 'package-1.conda', sha256: '1'.repeat(64), md5: '2'.repeat(32), license: 'MIT'};}
function diffRow(): LockfileDifference {return {ecosystem: 'conda', name: 'package', location: null, change: 'changed', before: diffEntry(), after: {...diffEntry(), build: '1', license: 'AGPL-3.0'}, changed_fields: ['build', 'license'], before_license: {declared: 'MIT', agpl: false}, after_license: {declared: 'AGPL-3.0', agpl: true}};}
function comparison(): LockfileDiffPage {
  const {before: a, after: b} = diffSelection();
  return {...common, before: {revision: a.revision, record_sha256: a.record_sha256, lockfile: a.lockfile}, after: {revision: b.revision, record_sha256: b.record_sha256, lockfile: b.lockfile}, status: 'complete', holds: [], before_header: {...a.lockfile!, python: null, platform: 'linux-64', skipped: 0}, after_header: {...b.lockfile!, python: '3.13', platform: 'linux-64', skipped: 0}, header_changes: ['python'], total: 1, added: 0, removed: 0, changed: 1, offset: 0, limit: 8, next_offset: null, differences: [diffRow()]};
}
describe('explicit pinned lockfile comparisons', () => {
  it('selects only exact two retained pins and allows same revision twice', () => {
    const s = diffSelection(); expect(provisionData(comparison(), scope, s)).toEqual(comparison());
    expect(provisionPath(scope, s)).toBe(`/api/v0/projects/p/environments/e/provisioning/observations/lockfile-diff?before=0&before_pin=${'a'.repeat(64)}&after=1&after_pin=${'b'.repeat(64)}&offset=0&limit=8`);
    expect(provisionPath(scope, {...s, after: s.before})).toContain('after=0');
    for (const offset of [-1,80001,true,2**53]) expect(() => provisionPath(scope, {...s, offset: offset as number})).toThrow();
  });
  it.each([
    ['scope',(v: LockfileDiffPage) => {v.environment_id='foreign';}],
    ['safe integer',(v: LockfileDiffPage) => {v.before.revision=2**53;}],
    ['raw pin',(v: LockfileDiffPage) => {v.before.record_sha256='f'.repeat(64);}],
    ['ref',(v: LockfileDiffPage) => {v.before.lockfile!.size++;}],
    ['header hash',(v: LockfileDiffPage) => {v.before_header!.sha256='f'.repeat(64);}],
    ['header count',(v: LockfileDiffPage) => {v.before_header!.conda_packages++;}],
    ['skipped',(v: LockfileDiffPage) => {v.before_header!.skipped=1;}],
    ['headers',(v: LockfileDiffPage) => {v.header_changes=[];}],
    ['offset',(v: LockfileDiffPage) => {v.offset=1;}],
    ['totals',(v: LockfileDiffPage) => {v.total=2;}],
    ['kind totals',(v: LockfileDiffPage) => {v.changed=0;v.added=1;}],
    ['bool',(v: LockfileDiffPage) => {v.total=true as unknown as number;}],
    ['continuation',(v: LockfileDiffPage) => {v.next_offset=1;}],
    ['missing rows',(v: LockfileDiffPage) => {v.differences=[];}],
    ['extra key',(v: LockfileDiffPage) => {Object.assign(v,{authority:'user'});}],
    ['changed fields',(v: LockfileDiffPage) => {v.differences[0]!.changed_fields.reverse();}],
    ['name',(v: LockfileDiffPage) => {v.differences[0]!.name='other';}],
    ['license',(v: LockfileDiffPage) => {v.differences[0]!.after_license!.declared='MIT';}],
    ['missing side',(v: LockfileDiffPage) => {v.differences[0]!.after=null;}],
  ])('refuses incoherent %s',(_name, change) => {const v=comparison();change(v);expect(() => provisionData(v,scope,diffSelection())).toThrow(ProvisionReadError);});
  it('keeps held inputs unknown and still binds available headers', () => {
    const held: LockfileDiffPage={...comparison(),status:'held',holds:[{side:'after',reason:'blob_missing'}],after_header:null,header_changes:[],total:null,added:null,removed:null,changed:null,next_offset:null,differences:[]};
    expect(provisionData(held,scope,diffSelection())).toEqual(held);
    for (const v of [{...held,total:0},{...held,holds:[]},{...held,before_header:{...held.before_header!,sha256:'f'.repeat(64)}},{...held,holds:[held.holds[0]!,held.holds[0]!]}]) expect(() => provisionData(v,scope,diffSelection())).toThrow();
    const html=render(EnvironmentPanel,{props:{projectId:'p',project:project(),initialSelection:diffSelection(),initialDetail:{phase:'observed',data:held,busy:false}}}).body.replace(/\s+/g, ' ');
    expect(html).toContain('package differences are unknown');expect(html).toContain('unavailable or evicted');expect(html).not.toContain('0 total differences');
  });
  it('distinguishes true identical/empty differences from unavailable inventory', () => {
    const s=diffSelection();s.after=s.before;const v={...comparison(),after:comparison().before,after_header:comparison().before_header,header_changes:[],total:0,changed:0,differences:[]};
    expect(provisionData(v,scope,s)).toEqual(v);
    const html=render(EnvironmentPanel,{props:{projectId:'p',project:project(),initialSelection:s,initialDetail:{phase:'observed',data:v,busy:false}}}).body.replace(/\s+/g, ' ');expect(html).toContain('No package-entry differences');expect(html).toContain('do not certify installed files');
  });
  it('keeps pip normalized-name/location and licenses unknown; escapes source text', () => {
    const row: LockfileDifference={ecosystem:'pip',name:'some-pkg',location:'user',change:'added',before:null,after:{name:'Some_Pkg',version:'1',installer:'uv',location:'user',url:'https://example.invalid/<script>',sha256:null},changed_fields:['installer','location','name','url','version'],before_license:null,after_license:{declared:null,agpl:null}};
    const selected=diffSelection();selected.after.lockfile!.pip_distributions=1;const base=comparison();base.after.lockfile!.pip_distributions=1;base.after_header!.pip_distributions=1;const v={...base,total:2,added:1,differences:[diffRow(),row]};expect(provisionData(v,scope,selected)).toEqual(v);
    expect(() => provisionData({...v,differences:[diffRow(),{...row,after_license:{declared:'MIT',agpl:false}}]},scope,selected)).toThrow();
    const html=render(EnvironmentPanel,{props:{projectId:'p',project:project(),initialSelection:diffSelection(),initialDetail:{phase:'observed',data:v,busy:false}}}).body.replace(/\s+/g, ' ');
    expect(html).toContain('AGPL status: flagged');expect(html).toContain('AGPL status: unknown');expect(html).toContain('md5');expect(html).toContain('&lt;script>');expect(html).not.toContain('<script>');expect(html).not.toContain('href="https://example');
  });
  it('follows actual short-page cursor; checks row order and pinned cross-page facts', () => {
    const a=diffRow(),b={...diffRow(),name:'second',before:{...diffEntry(),name:'second'},after:{...diffEntry(),name:'second',build:'1',license:'AGPL-3.0'}};
    const selected=diffSelection();selected.before.lockfile!.conda_packages=selected.after.lockfile!.conda_packages=3;const base=comparison();base.before.lockfile!.conda_packages=base.after.lockfile!.conda_packages=base.before_header!.conda_packages=base.after_header!.conda_packages=3;const first={...base,total:3,changed:3,next_offset:2,differences:[a,b]};expect(provisionData(first,scope,selected)).toMatchObject({next_offset:2});
    for (const rows of [[b,a],[a,a]]) expect(() => provisionData({...first,differences:rows},scope,selected)).toThrow();
    const c={...b,name:'third',before:{...b.before,name:'third'},after:{...b.after,name:'third'}};
    const last={...first,offset:2,next_offset:null,differences:[c]},s={...selected,offset:2,expected:diffIdentity(first),previousKey:differenceKey(b)};
    expect(provisionData(last,scope,s)).toEqual(last);expect(() => provisionData({...last,total:4,changed:4,next_offset:3},scope,s)).toThrow();expect(() => provisionData({...last,differences:[a]},scope,s)).toThrow();
  });
  it('labels dated records/backend proof without enabling actions or foreign data', () => {
    const props={projectId:'p',project:project(),initialSelection:diffSelection(),initialDetail:{phase:'unavailable' as const,data:comparison(),busy:false}};
    const html=render(EnvironmentPanel,{props}).body.replace(/\s+/g, ' ');expect(html).toContain('Previous selected record');expect(html).toContain('does not select newer sources');expect(html).toContain('verified by the backend');expect(html).not.toMatch(/<button[^>]*>\s*(Install|Approve|Cancel|Rebuild)/);
    expect(render(EnvironmentPanel,{props:{...props,initialDetail:{...props.initialDetail,data:{...comparison(),environment_id:'foreign'}}}}).body.replace(/\s+/g, ' ')).not.toContain('AGPL status: flagged');
  });
});
describe('comparison transport and one-shot ownership', () => {
  it('uses12s only for diff while sharing actual owner across ignored header abort', async () => {
    vi.useFakeTimers();const pending=deferred<Response>(),fetch=vi.fn().mockReturnValue(pending.promise);vi.stubGlobal('fetch',fetch);
    const first=readProvision(scope,diffSelection(),new AbortController().signal).catch(e=>e);
    await vi.advanceTimersByTimeAsync(PROVISION_DEADLINE_MS);expect(fetch.mock.calls[0]![1].signal.aborted).toBe(false);
    await expect(readProvision(scope,{kind:'summary'},new AbortController().signal)).rejects.toBeInstanceOf(ProvisionReadError);
    await vi.advanceTimersByTimeAsync(DIFF_DEADLINE_MS-PROVISION_DEADLINE_MS);expect(fetch.mock.calls[0]![1].signal.aborted).toBe(true);
    pending.resolve(new Response(JSON.stringify(comparison())));expect(await first).toBeInstanceOf(ProvisionReadError);expect(fetch).toHaveBeenCalledOnce();
    fetch.mockResolvedValue(new Response(JSON.stringify(overview())));expect(await readProvision(scope,{kind:'summary'},new AbortController().signal)).toEqual(overview());
  });
  it('keeps body-cancel lease until actual drain then permits history', async () => {
    vi.useFakeTimers();const drained=deferred<void>(),cancel=vi.fn().mockReturnValue(drained.promise),fetch=vi.fn().mockResolvedValueOnce(new Response(new ReadableStream({cancel}))).mockResolvedValue(new Response(JSON.stringify(history())));vi.stubGlobal('fetch',fetch);
    const first=readProvision(scope,diffSelection(),new AbortController().signal).catch(e=>e);await vi.advanceTimersByTimeAsync(DIFF_DEADLINE_MS);expect(cancel).toHaveBeenCalledOnce();
    await expect(readProvision(scope,{kind:'history',after:-1,target:null},new AbortController().signal)).rejects.toBeInstanceOf(ProvisionReadError);expect(fetch).toHaveBeenCalledOnce();
    drained.resolve();expect(await first).toBeInstanceOf(ProvisionReadError);expect(await readProvision(scope,{kind:'history',after:-1,target:null},new AbortController().signal)).toEqual(history());
  });
  it('settles one-shot after ignored timeout/hide; never repolls or publishes late data', async () => {
    vi.useFakeTimers();const pending=deferred<ProvisionData>(),states:ReadState[]=[],read=vi.fn().mockReturnValue(pending.promise),owner=new ProvisionReader(read,v=>states.push(v),false,DIFF_DEADLINE_MS,true);owners.push(owner);
    owner.setVisible(true);await vi.advanceTimersByTimeAsync(DIFF_DEADLINE_MS);expect(states.at(-1)).toMatchObject({phase:'unavailable',busy:true});owner.setVisible(false);owner.setVisible(true);pending.resolve(comparison());await vi.advanceTimersByTimeAsync(30000);
    expect(read).toHaveBeenCalledOnce();expect(states.at(-1)).toMatchObject({phase:'unavailable',busy:false});owner.setVisible(false);owner.setVisible(true);await vi.advanceTimersByTimeAsync(30000);expect(read).toHaveBeenCalledOnce();
  });
  it('never falls back after409 and bounds invalid UTF8/oversize comparison bodies', async () => {
    const fetch=vi.fn().mockResolvedValueOnce(new Response('arbitrary error',{status:409})).mockResolvedValueOnce(new Response(new Uint8Array([0xff]))).mockResolvedValueOnce(new Response(' '.repeat(65537)));vi.stubGlobal('fetch',fetch);
    await expect(readProvision(scope,diffSelection(),new AbortController().signal)).rejects.toMatchObject({status:409});expect(fetch).toHaveBeenCalledOnce();
    await expect(readProvision(scope,diffSelection(),new AbortController().signal)).rejects.toMatchObject({status:0});await expect(readProvision(scope,diffSelection(),new AbortController().signal)).rejects.toMatchObject({status:0});
  });
});

function packageSelection(): Extract<Selection, {kind: 'packages'}> {
  return {kind: 'packages', offset: 0, revision: {...history().revisions[0]!, lockfile: {sha256: 'd'.repeat(64), size: 1024, conda_packages: 2, pip_distributions: 1}}};
}
function packageInventory(): RevisionPackagePage {
  const s = packageSelection();
  return {...common, selection: {revision: s.revision.revision, record_sha256: s.revision.record_sha256, lockfile: s.revision.lockfile}, status: 'complete', holds: [], header: {...s.revision.lockfile!, python: '3.13', platform: 'linux-64', skipped: 0}, total: 3, offset: 0, limit: 8, next_offset: null, packages: [
    {ecosystem: 'conda', name: 'a', location: null, package: {...diffEntry(), name: 'a', license: null}, license: {declared: null, agpl: null}},
    {ecosystem: 'conda', name: 'unchanged', location: null, package: {...diffEntry(), name: 'unchanged', license: 'AGPL-3.0'}, license: {declared: 'AGPL-3.0', agpl: true}},
    {ecosystem: 'pip', name: 'pip-pkg', location: 'user', package: {name: 'Pip_Pkg', version: '1', installer: 'uv', location: 'user', url: 'https://example.invalid/<script>', sha256: null}, license: {declared: null, agpl: null}},
  ]};
}
describe('explicit selected revision recorded packages', () => {
  it('loads one historical raw pin without creating an empty comparison baseline', () => {
    const s = packageSelection(), v = packageInventory();
    expect(provisionPath(scope, s)).toBe(`/api/v0/projects/p/environments/e/provisioning/observations/revisions/1/packages?pin=${'b'.repeat(64)}&offset=0&limit=8`);
    expect(provisionData(v, scope, s)).toEqual(v);
    expect(v.packages[1]!.license.agpl).toBe(true);
    for (const offset of [true, -1, 40001, 1.5, 2**53]) expect(() => provisionPath(scope, {...s, offset: offset as number})).toThrow();
  });
  it.each([
    ['scope', (v: RevisionPackagePage) => {v.environment_id = 'foreign';}],
    ['raw pin', (v: RevisionPackagePage) => {v.selection.record_sha256 = 'a'.repeat(64);}],
    ['revision', (v: RevisionPackagePage) => {v.selection.revision = 2;}],
    ['ref', (v: RevisionPackagePage) => {v.selection.lockfile!.size++;}],
    ['header', (v: RevisionPackagePage) => {v.header!.sha256 = 'f'.repeat(64);}],
    ['skipped', (v: RevisionPackagePage) => {v.header!.skipped = 1;}],
    ['total', (v: RevisionPackagePage) => {v.total = 4;v.next_offset = 3;}],
    ['offset', (v: RevisionPackagePage) => {v.offset = 1;}],
    ['limit', (v: RevisionPackagePage) => {v.limit = 7;}],
    ['cursor', (v: RevisionPackagePage) => {v.next_offset = 3;}],
    ['duplicate', (v: RevisionPackagePage) => {v.packages[1] = v.packages[0]!;}],
    ['order', (v: RevisionPackagePage) => {v.packages.reverse();}],
    ['name', (v: RevisionPackagePage) => {v.packages[0]!.name = 'other';}],
    ['location', (v: RevisionPackagePage) => {v.packages[0]!.location = 'user';}],
    ['license', (v: RevisionPackagePage) => {v.packages[1]!.license.declared = 'MIT';}],
    ['AGPL', (v: RevisionPackagePage) => {v.packages[1]!.license.agpl = false;}],
    ['pip license', (v: RevisionPackagePage) => {v.packages[2]!.license = {declared: 'MIT', agpl: false};}],
    ['pip identity', (v: RevisionPackagePage) => {v.packages[2]!.name = 'Pip_Pkg';}],
  ])('refuses incoherent %s', (_name, change) => {const v = packageInventory();change(v);expect(() => provisionData(v, scope, packageSelection())).toThrow(ProvisionReadError);});
  it('keeps bool and unsafe numeric values out of packages and page metadata', () => {
    const raw = packageInventory();
    for (const changed of [{...raw, total: true}, {...raw, total: 2**53}, {...raw, offset: false}, {...raw, header: {...raw.header!, size: true}}, {...raw, packages: [{...raw.packages[0]!, license: {declared: null, agpl: false}}]}]) expect(() => provisionData(changed, scope, packageSelection())).toThrow();
  });
  it('follows actual short-page cursors and preserves full cross-page source facts', () => {
    const v = packageInventory(), first = {...v, packages: v.packages.slice(0, 2), next_offset: 2};
    expect(provisionData(first, scope, packageSelection())).toEqual(first);
    const last = {...v, offset: 2, packages: v.packages.slice(2)}, s = {...packageSelection(), offset: 2, expected: packageIdentity(first), previousKey: packageKey(first.packages.at(-1)!)};
    expect(provisionData(last, scope, s)).toEqual(last);
    expect(() => provisionData({...last, packages: [v.packages[0]!] }, scope, s)).toThrow();
    expect(() => provisionData({...last, header: {...last.header!, python: '3.14'}}, scope, s)).toThrow();
    expect(() => provisionData({...last, packages: []}, scope, s)).toThrow();
  });
  it('distinguishes held inventory from validated empty and binds held headers too', () => {
    const v = packageInventory(), held: RevisionPackagePage = {...v, status: 'held', holds: ['skipped_entries'], header: {...v.header!, skipped: 2}, total: null, next_offset: null, packages: []};
    expect(provisionData(held, scope, packageSelection())).toEqual(held);
    for (const changed of [{...held, total: 0}, {...held, holds: []}, {...held, packages: v.packages}, {...held, header: {...held.header!, size: 10}}, {...held, holds: ['blob_missing', 'input_limit']}]) expect(() => provisionData(changed, scope, packageSelection())).toThrow();
    const s = packageSelection();s.revision.lockfile!.conda_packages = s.revision.lockfile!.pip_distributions = 0;
    const empty = {...v, selection: {...v.selection, lockfile: s.revision.lockfile}, header: {...s.revision.lockfile!, python: null, platform: null, skipped: 0}, total: 0, packages: []};
    expect(provisionData(empty, scope, s)).toEqual(empty);
    const props = {projectId:'p', project:project(), initialSelection:packageSelection(), initialDetail:{phase:'observed' as const,data:held,busy:false}};
    expect(render(EnvironmentPanel,{props}).body.replace(/\s+/g, ' ')).toContain('packages and licenses are unknown');
    expect(render(EnvironmentPanel,{props}).body.replace(/\s+/g, ' ')).not.toContain('No package entries');
    expect(render(EnvironmentPanel,{props:{...props,initialSelection:s,initialDetail:{...props.initialDetail,data:empty}}}).body.replace(/\s+/g, ' ')).toContain('No package entries in this validated retained lockfile');
  });
  it('shows unchanged/base/unmanaged metadata, declared AGPL and unknown pip without activated URLs', () => {
    const s = packageSelection(), data = packageInventory();s.revision.revision = data.selection.revision = 0;
    const props = {projectId:'p',project:project(),initialSelection:s,initialDetail:{phase:'unavailable' as const,data,busy:false}};
    const html = render(EnvironmentPanel,{props}).body.replace(/\s+/g, ' ');
    expect(html).toContain('revision 0');expect(html).toContain('including unchanged packages');expect(html).toContain('AGPL flagged');expect(html).toContain('AGPL status: unknown');
    expect(html).toContain('Showing recorded packages 1–3 of 3');expect(html).toContain('Previous selected record');expect(html).toContain('do not certify installed files');
    expect(html).toContain('&lt;script>');expect(html).not.toContain('<script>');expect(html).not.toContain('href="https://example');expect(html).not.toMatch(/<button[^>]*>\s*(Install|Approve|Cancel|Rebuild)/);
    expect(render(EnvironmentPanel,{props:{...props,initialDetail:{...props.initialDetail,data:{...data,project_id:'foreign'}}}}).body.replace(/\s+/g, ' ')).not.toContain('AGPL flagged');
    const historyHtml = render(EnvironmentPanel,{props:{projectId:'p',project:project(),initialSelection:{kind:'history',after:-1,target:null},initialDetail:{phase:'observed',data:history(),busy:false}}}).body.replace(/\s+/g, ' ');
    expect(historyHtml).toContain('Recorded packages and licenses');
  });
});
describe('package pages share physical transport and one-shot visibility ownership', () => {
  it('uses12s package deadline and blocks all reader kinds while ignored abort owns fetch', async () => {
    vi.useFakeTimers();const pending = deferred<Response>(), fetch = vi.fn().mockReturnValue(pending.promise);vi.stubGlobal('fetch',fetch);
    const first = readProvision(scope,packageSelection(),new AbortController().signal).catch(e=>e);
    await vi.advanceTimersByTimeAsync(PROVISION_DEADLINE_MS);expect(fetch.mock.calls[0]![1].signal.aborted).toBe(false);
    await expect(readProvision(scope,diffSelection(),new AbortController().signal)).rejects.toBeInstanceOf(ProvisionReadError);
    await vi.advanceTimersByTimeAsync(PACKAGES_DEADLINE_MS-PROVISION_DEADLINE_MS);expect(fetch.mock.calls[0]![1].signal.aborted).toBe(true);
    pending.resolve(new Response(JSON.stringify(packageInventory())));expect(await first).toBeInstanceOf(ProvisionReadError);expect(fetch).toHaveBeenCalledOnce();
    fetch.mockResolvedValue(new Response(JSON.stringify(overview())));expect(await readProvision(scope,{kind:'summary'},new AbortController().signal)).toEqual(overview());
  });
  it('owns ignored body cancellation through drain across another project and selection', async () => {
    vi.useFakeTimers();const drain=deferred<void>(),cancel=vi.fn().mockReturnValue(drain.promise),fetch=vi.fn().mockResolvedValue(new Response(new ReadableStream({cancel})));vi.stubGlobal('fetch',fetch);
    const pending=readProvision(scope,packageSelection(),new AbortController().signal).catch(e=>e);await vi.advanceTimersByTimeAsync(PACKAGES_DEADLINE_MS);expect(cancel).toHaveBeenCalledOnce();
    await expect(readProvision({projectId:'q',environmentId:'f'},{kind:'history',after:-1,target:null},new AbortController().signal)).rejects.toBeInstanceOf(ProvisionReadError);expect(fetch).toHaveBeenCalledOnce();
    drain.resolve();expect(await pending).toBeInstanceOf(ProvisionReadError);
  });
  it('never repolls a selected page after ignored timeout/hide/show and rejects late data', async () => {
    vi.useFakeTimers();const pending=deferred<ProvisionData>(),states:ReadState[]=[],read=vi.fn().mockReturnValue(pending.promise),owner=new ProvisionReader(read,v=>states.push(v),false,PACKAGES_DEADLINE_MS,true);owners.push(owner);
    owner.setVisible(true);await vi.advanceTimersByTimeAsync(PACKAGES_DEADLINE_MS);owner.setVisible(false);owner.setVisible(true);pending.resolve(packageInventory());await vi.advanceTimersByTimeAsync(30_000);
    expect(read).toHaveBeenCalledOnce();expect(states.at(-1)).toMatchObject({phase:'unavailable',busy:false,data:null});owner.setVisible(false);owner.setVisible(true);await vi.advanceTimersByTimeAsync(30_000);expect(read).toHaveBeenCalledOnce();
  });
  it('keeps stale pin409 explicit with no fallback and rejects invalid UTF8/overlarge bytes', async () => {
    const fetch=vi.fn().mockResolvedValueOnce(new Response('raw error',{status:409})).mockResolvedValueOnce(new Response(new Uint8Array([0xff]))).mockResolvedValueOnce(new Response(' '.repeat(65537)));vi.stubGlobal('fetch',fetch);
    await expect(readProvision(scope,packageSelection(),new AbortController().signal)).rejects.toMatchObject({status:409});expect(fetch).toHaveBeenCalledOnce();
    await expect(readProvision(scope,packageSelection(),new AbortController().signal)).rejects.toMatchObject({status:0});await expect(readProvision(scope,packageSelection(),new AbortController().signal)).rejects.toMatchObject({status:0});
  });
  it('logout prevents late package publication and any new actual read owner', async () => {
    vi.resetModules();const transport=await import('./provisioning'),auth=await import('./kernels'),pending=deferred<Response>(),fetch=vi.fn().mockReturnValue(pending.promise);vi.stubGlobal('fetch',fetch);
    const read=transport.readProvision(scope,packageSelection(),new AbortController().signal).catch(e=>e);auth.stopKernelReadsForLogout();pending.resolve(new Response(JSON.stringify(packageInventory())));
    expect(await read).toBeInstanceOf(transport.ProvisionReadError);await expect(transport.readProvision(scope,packageSelection(),new AbortController().signal)).rejects.toMatchObject({status:401});expect(fetch).toHaveBeenCalledOnce();vi.resetModules();
  });
});

describe('additive recorded pip license declarations', () => {
  it.each([null, 'MIT', 'AGPL-3.0-only'])('accepts explicit %s and binds declaration/flag to the pip entry', license => {
    const v = packageInventory(), row = v.packages[2]!;
    row.package = {...row.package, license}; row.license = {declared: license, agpl: license === null ? null : /agpl|affero/i.test(license)};
    expect(provisionData(v, scope, packageSelection())).toEqual(v);
    const wrong = structuredClone(v); wrong.packages[2]!.license.declared = 'BSD';
    expect(() => provisionData(wrong, scope, packageSelection())).toThrow();
    if (license !== null) {const bad = structuredClone(v);bad.packages[2]!.license.agpl = !bad.packages[2]!.license.agpl;expect(() => provisionData(bad, scope, packageSelection())).toThrow();}
  });
  it('renders declared pip metadata without the stale always-unknown caption', () => {
    const data = packageInventory(), row = data.packages[2]!;row.package = {...row.package,license:'AGPL-3.0-only'};row.license = {declared:'AGPL-3.0-only',agpl:true};
    const html = render(EnvironmentPanel,{props:{projectId:'p',project:project(),initialSelection:packageSelection(),initialDetail:{phase:'observed',data,busy:false}}}).body.replace(/\s+/g, ' ');
    expect(html).toContain('AGPL-3.0-only');expect(html).toContain('Missing declarations remain unknown');expect(html).not.toContain('Pip licenses remain unknown');
  });
  it('retains absent legacy license as unknown without accepting unrelated extra fields', () => {
    const v = packageInventory();expect(provisionData(v, scope, packageSelection())).toEqual(v);
    const extra = structuredClone(v) as unknown as {packages: {package: Record<string, unknown>}[]};extra.packages[2]!.package['unrelated'] = 'value';
    expect(() => provisionData(extra, scope, packageSelection())).toThrow();
  });
  it.each([true, 42, [], {}, '', 'L'.repeat(257), 'MIT\u0000', 'MIT\nBSD'])('refuses malformed recorded license %j', license => {
    const v = packageInventory() as unknown as {packages: {package: Record<string, unknown>;license: unknown}[]};
    v.packages[2]!.package['license'] = license;v.packages[2]!.license = {declared: license, agpl: false};
    expect(() => provisionData(v, scope, packageSelection())).toThrow();
  });
  it('accepts license-only pip changes and refuses detached side declarations', () => {
    const v = comparison(), before = {name:'pip-pkg', version:'1', installer:'pip', location:'prefix' as const, url:null, sha256:null, license:'MIT'}, after = {...before,license:'AGPL-3.0-only'};
    v.differences = [{ecosystem:'pip',name:'pip-pkg',location:'prefix',change:'changed',before,after,changed_fields:['license'],before_license:{declared:'MIT',agpl:false},after_license:{declared:'AGPL-3.0-only',agpl:true}}];
    expect(provisionData(v, scope, diffSelection())).toEqual(v);
    v.differences[0]!.after_license!.declared = 'MIT';
    expect(() => provisionData(v, scope, diffSelection())).toThrow();
  });
});


describe('recorded declaration flag consistency', () => {
  it.each(['MIT','AGPL-3.0-only','GNU Affero GPL'])('refuses inverted %s flags on both pip difference sides', license => {
    const v = comparison(), before = {name:'pip-pkg',version:'1',installer:'pip',location:'prefix' as const,url:null,sha256:null,license}, after = {...before,version:'2'}, agpl = /agpl|affero/i.test(license);
    v.differences = [{ecosystem:'pip',name:'pip-pkg',location:'prefix',change:'changed',before,after,changed_fields:['version'],before_license:{declared:license,agpl},after_license:{declared:license,agpl}}];
    expect(provisionData(v,scope,diffSelection())).toEqual(v);
    for (const side of ['before_license','after_license'] as const) {const copied = structuredClone(v);copied.differences[0]![side]!.agpl = !agpl;expect(() => provisionData(copied,scope,diffSelection())).toThrow();}
  });
});


describe('explicit Conda upgrade request observations', () => {
  it('accepts legacy install detail and explicit named Conda upgrade', () => {
    const old = detail();
    expect(provisionData(old, scope, {kind: 'operation', operation: op()})).toEqual(old);
    const upgrade = {...old, request: {...old.request, action: 'upgrade'}};
    expect(provisionData(upgrade, scope, {kind: 'operation', operation: op()})).toEqual(upgrade);
  });
  it.each([null, true, 1, 'remove', 'restore', 'upgrade-all'])('refuses unsupported action %s', action => {
    const old = detail();
    expect(() => provisionData({...old, request: {...old.request, action}}, scope, {kind: 'operation', operation: op()})).toThrow();
  });
  it('refuses upgrade on pip, capability or mixed system input', () => {
    const old = detail();
    for (const fields of [{conda: [], pip: ['example'], pip_reason: 'synthetic'}, {conda: [], capability: 'gp'}, {system: ['libgmp-dev']}]) {
      expect(() => provisionData({...old, request: {...old.request, action: 'upgrade', ...fields}}, scope, {kind: 'operation', operation: op()})).toThrow();
    }
  });
});
