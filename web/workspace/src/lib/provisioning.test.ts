// SPDX-License-Identifier: MIT
// Rendered HTML is compared with each whitespace run as one space, so that line breaks in
// the components' source do not matter.
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Project } from './api';
import EnvironmentPanel from '../views/EnvironmentPanel.svelte';
import { ApiError } from './http';
import { Poller, type PollState } from './poller';
import {
  environmentChoices,
  overviewPoll,
  type ProvisionData,
  type ProvisionObservation,
  type OperationSummary,
  type OperationDetail,
  type RevisionPackagePage,
  type LockfileDiffPage,
  type CondaEntry,
  type LockfileDifference,
  type Selection,
  type FilePage,
  type HistoryPage,
  type ProbePage,
} from './provisioning';
const scope = { projectId: 'p', environmentId: 'e' },
  time = '2026-10-13T06:00:00Z';
const common = { project_id: 'p', environment_id: 'e', observed_at: time };
function project(): Project {
  return {
    project_id: 'p',
    profile: 'minimal',
    policy: 'sha256:' + 'a'.repeat(64),
    created_at: time,
    updated_at: time,
    default_environment_id: 'e',
    sessions: [
      {
        session_id: 's',
        environment_id: 'e',
        dedicated: false,
        opened_at: time,
        closed_at: null,
        runs: 0,
      },
    ],
    environments: [{ environment_id: 'e', default: true, sessions: ['s'] }],
    model_cost: {
      last_day: null,
      total: null,
      by_role: [],
      model_info_last_day: null,
      model_info_total: null,
    },
  };
}
function op(): OperationSummary {
  return {
    provision_id: 'prv-one',
    operation_id: 'op-one',
    session_id: 's',
    seq: 3,
    state: 'awaiting_permission',
    outcome: 'awaiting_permission',
    reason: null,
    stage: 'permission',
    planned_revision: 0,
    revision_before: 0,
    revision_after: null,
    permission_request: 7,
    submitted_at: time,
    started_at: time,
    ended_at: null,
    files_total: 2,
    changes_total: 0,
    probes_total: 0,
    executions_total: 1,
    notes_total: 0,
  };
}
function overview(): ProvisionObservation {
  return {
    ...common,
    gate: { generation: 1, mode: 'ready', operation: null, revision: 0, recorded: true },
    current_revision: 0,
    queue_total: 1,
    operations: [op()],
  };
}
function detail(): OperationDetail {
  return {
    ...common,
    summary: op(),
    request: {
      operation_id: 'op-one',
      context: {
        project_id: 'p',
        session_id: 's',
        environment_id: 'e',
        policy_revision: null,
        actor: 'agent',
      },
      purpose: '<img src=x onerror=attack()>',
      capability: null,
      conda: ['networkx'],
      pip: [],
      pip_reason: null,
      system: [],
      action: 'install',
    },
    policy: null,
    catalog: null,
    next_step: 'Ask the operator',
    error_present: false,
    error_kind: null,
  };
}
function files(): FilePage {
  return {
    ...common,
    provision_id: 'prv-one',
    seq: 3,
    total: 2,
    offset: 0,
    limit: 8,
    next_offset: null,
    files: [
      {
        ecosystem: 'conda',
        name: 'example',
        version: '1',
        build: null,
        channel: 'conda-forge',
        subdir: 'linux-64',
        file: 'example-1.conda',
        url: null,
        sha256: null,
        license: null,
        agpl: false,
        action: 'escalate',
        rule: 'packages.conda',
      },
      {
        ecosystem: 'pypi',
        name: 'other',
        version: '2',
        build: null,
        channel: null,
        subdir: null,
        file: 'other.whl',
        url: 'https://example.invalid/<script>',
        sha256: null,
        license: 'AGPL-3.0',
        agpl: true,
        action: 'deny',
        rule: null,
      },
    ],
  };
}
function history(): HistoryPage {
  return {
    ...common,
    retained_total: 2,
    first_retained: 0,
    target: 1,
    after: -1,
    limit: 8,
    next_after: null,
    revisions: [
      {
        revision: 1,
        record_sha256: 'b'.repeat(64),
        fingerprint: 'c'.repeat(32),
        managed: false,
        changed_by: null,
        operation: null,
        recorded_at: time,
        lockfile: null,
        commands_total: 0,
        omitted_commands: 0,
        probes_total: 1,
      },
    ],
  };
}
function probes(): ProbePage {
  return {
    ...common,
    revision: 1,
    record_sha256: 'b'.repeat(64),
    total: 1,
    offset: 0,
    limit: 8,
    next_offset: null,
    probes: [
      {
        capability: null,
        probe: 'installed:example',
        version: 1,
        kind: 'import_check',
        status: 'passed',
        revision: null,
        execution_id: null,
        output: '<script>attack()</script>',
        duration_seconds: 0,
        operation: null,
        recorded_at: time,
      },
    ],
  };
}
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('canonical read scopes and pinned DTOs', () => {
  it('retains recorded dedicated closed environments without reopen authority', () => {
    const p = project();
    p.sessions.push({
      session_id: 'closed',
      environment_id: 'd',
      dedicated: true,
      opened_at: time,
      closed_at: time,
      runs: 0,
    });
    p.environments.push({ environment_id: 'd', default: false, sessions: ['closed'] });
    expect(environmentChoices('p', p)).toHaveLength(2);
    expect(environmentChoices('foreign', p)).toEqual([]);
  });
});

describe('an environment that is not made yet, and one that is gone', () => {
  /** The project's shared environment with `runs` runs in its one session. */
  const ranIn = (runs: number): Project => {
    const p = project();
    p.sessions[0]!.runs = runs;
    return p;
  };
  const notFound = new ApiError(404, 'not_found', 'no retained provisioning observation');
  const overviewOf = (runs: number) => overviewPoll(environmentChoices('p', ranIn(runs))[0]!);
  const settled = (states: PollState<ProvisionData>[]) => states.filter((state) => !state.busy);

  it('is known to have been made once any session using it has a run', () => {
    expect(environmentChoices('p', ranIn(0))[0]!.ran).toBe(false);
    expect(environmentChoices('p', ranIn(1))[0]!.ran).toBe(true);
    // A run in a session of another environment says nothing about this one.
    const p = ranIn(0);
    p.sessions.push({ ...p.sessions[0]!, session_id: 'x', environment_id: 'd', runs: 4 });
    p.environments.push({ environment_id: 'd', default: false, sessions: ['x'] });
    expect(environmentChoices('p', p).map((choice) => choice.ran)).toEqual([false, true]);
  });

  it('keeps asking for one not made yet, and ends the poll for one that is gone', async () => {
    vi.useFakeTimers();
    for (const [runs, reads] of [
      [0, 4],
      [1, 1],
    ] as const) {
      const read = vi.fn().mockRejectedValue(notFound);
      const states: PollState<ProvisionData>[] = [];
      const poller = new Poller(read, (state) => states.push(state), overviewOf(runs));
      poller.setVisible(true);
      await vi.advanceTimersByTimeAsync(10_000);
      expect(read).toHaveBeenCalledTimes(reads);
      expect(settled(states).at(-1)).toEqual({ phase: 'missing', data: null, busy: false });
      poller.stop();
    }
  });

  it('shows the environment once the first run makes it', async () => {
    vi.useFakeTimers();
    const read = vi.fn().mockRejectedValueOnce(notFound).mockResolvedValue(overview());
    const states: PollState<ProvisionData>[] = [];
    const poller = new Poller(read, (state) => states.push(state), overviewOf(0));
    poller.setVisible(true);
    await vi.advanceTimersByTimeAsync(3100);
    expect(settled(states).at(-1)).toMatchObject({ phase: 'observed', data: overview() });
    poller.stop();
  });

  it('tells them apart in words', () => {
    const view = (runs: number) =>
      render(EnvironmentPanel, {
        props: {
          projectId: 'p',
          project: ranIn(runs),
          initialView: { phase: 'missing', data: null, busy: false },
        },
      }).body.replace(/\s+/g, ' ');
    expect(view(0)).toContain('has not been created yet');
    expect(view(0)).not.toContain('no longer available');
    expect(view(1)).toContain('no longer available');
    expect(view(1)).not.toContain('not been created');
  });
});

describe('escaped read-only view', () => {
  it('shows project-wide queue, independent VM/package state and dated previous view', () => {
    const html = render(EnvironmentPanel, {
      props: {
        projectId: 'p',
        project: project(),
        initialView: { phase: 'unavailable', data: overview(), busy: false },
      },
    }).body.replace(/\s+/g, ' ');
    expect(html).toContain('Previous observation');
    expect(html).toContain('VM state');
    expect(html).toContain('not observed');
    expect(html).toContain('Permission request 7');
    expect(html).toContain('whole project environment');
    expect(html).not.toMatch(/<button[^>]*>\s*(Approve|Deny|Install|Cancel|Rebuild)/);
  });
  it('shows exact file count, unknown declared licenses and AGPL without HTML activation', () => {
    const html = render(EnvironmentPanel, {
      props: {
        projectId: 'p',
        project: project(),
        initialView: { phase: 'observed', data: overview(), busy: false },
        initialSelection: { kind: 'files', operation: op(), offset: 0 },
        initialDetail: { phase: 'observed', data: files(), busy: false },
      },
    }).body.replace(/\s+/g, ' ');
    expect(html).toContain('Showing files 1–2 of 2');
    expect(html).toContain('unknown');
    expect(html).toContain('AGPL flagged');
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script>');
  });
  it('keeps probe attribution and import-check limitations literal', () => {
    const html = render(EnvironmentPanel, {
      props: {
        projectId: 'p',
        project: project(),
        initialSelection: { kind: 'probes', revision: history().revisions[0]!, offset: 0 },
        initialDetail: { phase: 'observed', data: probes(), busy: false },
      },
    }).body.replace(/\s+/g, ' ');
    expect(html).toContain('Revision attribution unknown');
    expect(html).toContain('does not prove functional capability');
    expect(html).toContain('&lt;script>');
    expect(html).not.toContain('<script>');
  });
});

describe('complete pagination and lifecycle boundaries', () => {
  it('keeps a nine-file set distinct from its first eight-file page', () => {
    const operation = { ...op(), files_total: 9 };
    const first = {
      ...files(),
      total: 9,
      next_offset: 8,
      files: Array.from({ length: 8 }, (_, i) => ({ ...files().files[0]!, name: `package${i}` })),
    };
    const last = {
      ...files(),
      total: 9,
      offset: 8,
      files: [{ ...files().files[0]!, name: 'package8' }],
    };
    const html = render(EnvironmentPanel, {
      props: {
        projectId: 'p',
        project: project(),
        initialView: {
          phase: 'observed',
          data: { ...overview(), operations: [operation] },
          busy: false,
        },
        initialSelection: { kind: 'files', operation, offset: 0 },
        initialDetail: { phase: 'observed', data: first, busy: false },
      },
    }).body.replace(/\s+/g, ' ');
    expect(html).toContain('Showing files 1–8 of 9');
    expect(html).toContain('This is a page of the file set');
    expect(html).not.toContain('package8');
  });
  it('marks a selected departed operation historical and hides foreign selected facts', () => {
    const props = {
      projectId: 'p',
      project: project(),
      initialView: {
        phase: 'observed' as const,
        data: { ...overview(), queue_total: 0, operations: [] },
        busy: false,
      },
      initialSelection: { kind: 'operation' as const, operation: op() },
      initialDetail: { phase: 'observed' as const, data: detail(), busy: false },
    };
    expect(render(EnvironmentPanel, { props }).body.replace(/\s+/g, ' ')).toContain(
      'This selected record is historical',
    );
    expect(
      render(EnvironmentPanel, {
        props: {
          ...props,
          initialDetail: {
            ...props.initialDetail,
            data: { ...detail(), environment_id: 'foreign' },
          },
        },
      }).body.replace(/\s+/g, ' '),
    ).not.toContain('Ask the operator');
  });
});

describe('explicit current retained operations', () => {
  it('labels retained operation state and leaves its file pagination available outside the queue', () => {
    const empty = { ...overview(), queue_total: 0, operations: [] };
    const props = {
      projectId: 'p',
      project: project(),
      initialView: { phase: 'observed' as const, data: empty, busy: false },
      initialSelection: { kind: 'retained_operation' as const, provisionId: 'prv-one' },
      initialDetail: { phase: 'observed' as const, data: detail(), busy: false },
    };
    const html = render(EnvironmentPanel, { props }).body.replace(/\s+/g, ' ');
    expect(html).toContain('Current retained operation state');
    expect(html).toContain('does not reproduce its state at that revision');
    expect(html).not.toContain('This selected record is historical');
    const fileHtml = render(EnvironmentPanel, {
      props: {
        ...props,
        initialSelection: { kind: 'files', operation: op(), offset: 0, retained: true },
        initialDetail: { ...props.initialDetail, data: files() },
      },
    }).body.replace(/\s+/g, ' ');
    expect(fileHtml).toContain('explicitly loaded current retained operation');
    expect(fileHtml).not.toContain('This selected record is historical');
    const historyHtml = render(EnvironmentPanel, {
      props: {
        ...props,
        initialSelection: { kind: 'history', after: -1, target: null },
        initialDetail: {
          ...props.initialDetail,
          data: { ...history(), revisions: [{ ...history().revisions[0]!, operation: 'prv-one' }] },
        },
      },
    }).body.replace(/\s+/g, ' ');
    expect(historyHtml).toContain('Load current retained operation');
  });
});

describe('recorded permission references', () => {
  it('does not infer an unanswered permission from a retained reference during installation', () => {
    const data = {
      ...overview(),
      operations: [
        { ...op(), state: 'installing' as const, stage: 'link' as const, outcome: null },
      ],
    };
    const html = render(EnvironmentPanel, {
      props: {
        projectId: 'p',
        project: project(),
        initialView: { phase: 'observed', data, busy: false },
      },
    }).body.replace(/\s+/g, ' ');
    expect(html).toContain('recorded reference, current decision not shown');
    expect(html).not.toContain('waiting for an operator answer');
  });
});

function diffSelection(): Extract<Selection, { kind: 'diff' }> {
  const row = history().revisions[0]!;
  return {
    kind: 'diff',
    offset: 0,
    before: {
      ...row,
      revision: 0,
      record_sha256: 'a'.repeat(64),
      lockfile: { sha256: 'd'.repeat(64), size: 128, conda_packages: 1, pip_distributions: 0 },
    },
    after: {
      ...row,
      lockfile: { sha256: 'e'.repeat(64), size: 129, conda_packages: 1, pip_distributions: 0 },
    },
  };
}
function diffEntry(): CondaEntry {
  return {
    name: 'package',
    version: '1',
    build: '0',
    channel: 'conda-forge',
    subdir: 'linux-64',
    file: 'package-1.conda',
    sha256: '1'.repeat(64),
    md5: '2'.repeat(32),
    license: 'MIT',
  };
}
function diffRow(): LockfileDifference {
  return {
    ecosystem: 'conda',
    name: 'package',
    location: null,
    change: 'changed',
    before: diffEntry(),
    after: { ...diffEntry(), build: '1', license: 'AGPL-3.0' },
    changed_fields: ['build', 'license'],
    before_license: { declared: 'MIT', agpl: false },
    after_license: { declared: 'AGPL-3.0', agpl: true },
  };
}
function comparison(): LockfileDiffPage {
  const { before: a, after: b } = diffSelection();
  return {
    ...common,
    before: { revision: a.revision, record_sha256: a.record_sha256, lockfile: a.lockfile },
    after: { revision: b.revision, record_sha256: b.record_sha256, lockfile: b.lockfile },
    status: 'complete',
    holds: [],
    before_header: { ...a.lockfile!, python: null, platform: 'linux-64', skipped: 0 },
    after_header: { ...b.lockfile!, python: '3.13', platform: 'linux-64', skipped: 0 },
    header_changes: ['python'],
    total: 1,
    added: 0,
    removed: 0,
    changed: 1,
    offset: 0,
    limit: 8,
    next_offset: null,
    differences: [diffRow()],
  };
}
describe('explicit pinned lockfile comparisons', () => {
  it('keeps held inputs unknown and still binds available headers', () => {
    const held: LockfileDiffPage = {
      ...comparison(),
      status: 'held',
      holds: [{ side: 'after', reason: 'blob_missing' }],
      after_header: null,
      header_changes: [],
      total: null,
      added: null,
      removed: null,
      changed: null,
      next_offset: null,
      differences: [],
    };
    const html = render(EnvironmentPanel, {
      props: {
        projectId: 'p',
        project: project(),
        initialSelection: diffSelection(),
        initialDetail: { phase: 'observed', data: held, busy: false },
      },
    }).body.replace(/\s+/g, ' ');
    expect(html).toContain('package differences are unknown');
    expect(html).toContain('unavailable or evicted');
    expect(html).not.toContain('0 total differences');
  });
  it('distinguishes true identical/empty differences from unavailable inventory', () => {
    const s = diffSelection();
    s.after = s.before;
    const v = {
      ...comparison(),
      after: comparison().before,
      after_header: comparison().before_header,
      header_changes: [],
      total: 0,
      changed: 0,
      differences: [],
    };
    const html = render(EnvironmentPanel, {
      props: {
        projectId: 'p',
        project: project(),
        initialSelection: s,
        initialDetail: { phase: 'observed', data: v, busy: false },
      },
    }).body.replace(/\s+/g, ' ');
    expect(html).toContain('No package-entry differences');
    expect(html).toContain('do not certify installed files');
  });
  it('keeps pip normalized-name/location and licenses unknown; escapes source text', () => {
    const row: LockfileDifference = {
      ecosystem: 'pip',
      name: 'some-pkg',
      location: 'user',
      change: 'added',
      before: null,
      after: {
        name: 'Some_Pkg',
        version: '1',
        installer: 'uv',
        location: 'user',
        url: 'https://example.invalid/<script>',
        sha256: null,
        license: null,
      },
      changed_fields: ['installer', 'location', 'name', 'url', 'version'],
      before_license: null,
      after_license: { declared: null, agpl: null },
    };
    const selected = diffSelection();
    selected.after.lockfile!.pip_distributions = 1;
    const base = comparison();
    base.after.lockfile!.pip_distributions = 1;
    base.after_header!.pip_distributions = 1;
    const v = { ...base, total: 2, added: 1, differences: [diffRow(), row] };
    const html = render(EnvironmentPanel, {
      props: {
        projectId: 'p',
        project: project(),
        initialSelection: diffSelection(),
        initialDetail: { phase: 'observed', data: v, busy: false },
      },
    }).body.replace(/\s+/g, ' ');
    expect(html).toContain('AGPL status: flagged');
    expect(html).toContain('AGPL status: unknown');
    expect(html).toContain('md5');
    expect(html).toContain('&lt;script>');
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('href="https://example');
  });
  it('labels dated records/backend proof without enabling actions or foreign data', () => {
    const props = {
      projectId: 'p',
      project: project(),
      initialSelection: diffSelection(),
      initialDetail: { phase: 'unavailable' as const, data: comparison(), busy: false },
    };
    const html = render(EnvironmentPanel, { props }).body.replace(/\s+/g, ' ');
    expect(html).toContain('Previous selected record');
    expect(html).toContain('does not select newer sources');
    expect(html).toContain('verified by the backend');
    expect(html).not.toMatch(/<button[^>]*>\s*(Install|Approve|Cancel|Rebuild)/);
    expect(
      render(EnvironmentPanel, {
        props: {
          ...props,
          initialDetail: {
            ...props.initialDetail,
            data: { ...comparison(), environment_id: 'foreign' },
          },
        },
      }).body.replace(/\s+/g, ' '),
    ).not.toContain('AGPL status: flagged');
  });
});
function packageSelection(): Extract<Selection, { kind: 'packages' }> {
  return {
    kind: 'packages',
    offset: 0,
    revision: {
      ...history().revisions[0]!,
      lockfile: { sha256: 'd'.repeat(64), size: 1024, conda_packages: 2, pip_distributions: 1 },
    },
  };
}
function packageInventory(): RevisionPackagePage {
  const s = packageSelection();
  return {
    ...common,
    selection: {
      revision: s.revision.revision,
      record_sha256: s.revision.record_sha256,
      lockfile: s.revision.lockfile,
    },
    status: 'complete',
    holds: [],
    header: { ...s.revision.lockfile!, python: '3.13', platform: 'linux-64', skipped: 0 },
    total: 3,
    offset: 0,
    limit: 8,
    next_offset: null,
    packages: [
      {
        ecosystem: 'conda',
        name: 'a',
        location: null,
        package: { ...diffEntry(), name: 'a', license: null },
        license: { declared: null, agpl: null },
      },
      {
        ecosystem: 'conda',
        name: 'unchanged',
        location: null,
        package: { ...diffEntry(), name: 'unchanged', license: 'AGPL-3.0' },
        license: { declared: 'AGPL-3.0', agpl: true },
      },
      {
        ecosystem: 'pip',
        name: 'pip-pkg',
        location: 'user',
        package: {
          name: 'Pip_Pkg',
          version: '1',
          installer: 'uv',
          location: 'user',
          url: 'https://example.invalid/<script>',
          sha256: null,
          license: null,
        },
        license: { declared: null, agpl: null },
      },
    ],
  };
}
describe('explicit selected revision recorded packages', () => {
  it('distinguishes held inventory from validated empty and binds held headers too', () => {
    const v = packageInventory(),
      held: RevisionPackagePage = {
        ...v,
        status: 'held',
        holds: ['skipped_entries'],
        header: { ...v.header!, skipped: 2 },
        total: null,
        next_offset: null,
        packages: [],
      };
    const s = packageSelection();
    s.revision.lockfile!.conda_packages = s.revision.lockfile!.pip_distributions = 0;
    const empty = {
      ...v,
      selection: { ...v.selection, lockfile: s.revision.lockfile },
      header: { ...s.revision.lockfile!, python: null, platform: null, skipped: 0 },
      total: 0,
      packages: [],
    };
    const props = {
      projectId: 'p',
      project: project(),
      initialSelection: packageSelection(),
      initialDetail: { phase: 'observed' as const, data: held, busy: false },
    };
    expect(render(EnvironmentPanel, { props }).body.replace(/\s+/g, ' ')).toContain(
      'packages and licenses are unknown',
    );
    expect(render(EnvironmentPanel, { props }).body.replace(/\s+/g, ' ')).not.toContain(
      'No package entries',
    );
    expect(
      render(EnvironmentPanel, {
        props: {
          ...props,
          initialSelection: s,
          initialDetail: { ...props.initialDetail, data: empty },
        },
      }).body.replace(/\s+/g, ' '),
    ).toContain('No package entries in this validated retained lockfile');
  });
  it('shows unchanged/base/unmanaged metadata, declared AGPL and unknown pip without activated URLs', () => {
    const s = packageSelection(),
      data = packageInventory();
    s.revision.revision = data.selection.revision = 0;
    const props = {
      projectId: 'p',
      project: project(),
      initialSelection: s,
      initialDetail: { phase: 'unavailable' as const, data, busy: false },
    };
    const html = render(EnvironmentPanel, { props }).body.replace(/\s+/g, ' ');
    expect(html).toContain('revision 0');
    expect(html).toContain('including unchanged packages');
    expect(html).toContain('AGPL flagged');
    expect(html).toContain('AGPL status: unknown');
    expect(html).toContain('Showing recorded packages 1–3 of 3');
    expect(html).toContain('Previous selected record');
    expect(html).toContain('do not certify installed files');
    expect(html).toContain('&lt;script>');
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('href="https://example');
    expect(html).not.toMatch(/<button[^>]*>\s*(Install|Approve|Cancel|Rebuild)/);
    expect(
      render(EnvironmentPanel, {
        props: {
          ...props,
          initialDetail: { ...props.initialDetail, data: { ...data, project_id: 'foreign' } },
        },
      }).body.replace(/\s+/g, ' '),
    ).not.toContain('AGPL flagged');
    const historyHtml = render(EnvironmentPanel, {
      props: {
        projectId: 'p',
        project: project(),
        initialSelection: { kind: 'history', after: -1, target: null },
        initialDetail: { phase: 'observed', data: history(), busy: false },
      },
    }).body.replace(/\s+/g, ' ');
    expect(historyHtml).toContain('Recorded packages and licenses');
  });
});
describe('additive recorded pip license declarations', () => {
  it('renders declared pip metadata without the stale always-unknown caption', () => {
    const data = packageInventory(),
      row = data.packages[2]!;
    row.package = { ...row.package, license: 'AGPL-3.0-only' };
    row.license = { declared: 'AGPL-3.0-only', agpl: true };
    const html = render(EnvironmentPanel, {
      props: {
        projectId: 'p',
        project: project(),
        initialSelection: packageSelection(),
        initialDetail: { phase: 'observed', data, busy: false },
      },
    }).body.replace(/\s+/g, ' ');
    expect(html).toContain('AGPL-3.0-only');
    expect(html).toContain('Missing declarations remain unknown');
    expect(html).not.toContain('Pip licenses remain unknown');
  });
});
