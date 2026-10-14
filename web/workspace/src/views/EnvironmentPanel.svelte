<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import type { Project } from '../lib/api';
  import { formatTime } from '../lib/format';
  import { onKernelLogout } from '../lib/kernels';
  import { environmentChoices, ProvisionReader, readProvision, diffIdentity, differenceKey, packageIdentity, packageKey, DIFF_DEADLINE_MS, PACKAGES_DEADLINE_MS, PROVISION_DEADLINE_MS, type ReadState, type Selection,
    type ProvisionObservation, type OperationDetail, type FilePage, type HistoryPage, type ProbePage, type RevisionSummary, type LockfileDiffPage, type RevisionPackagePage } from '../lib/provisioning';

  let {projectId, project, initialView = {phase: 'loading', data: null, busy: false}, initialSelection = null, initialDetail = {phase: 'loading', data: null, busy: false}}:
    {projectId: string; project: Project; initialView?: ReadState; initialSelection?: Selection | null; initialDetail?: ReadState} = $props();
  const choices = $derived(environmentChoices(projectId, project));
  let selectedId = $state<string | null>(null);
  const selected = $derived(choices.find(e => e.scope.environmentId === selectedId) ?? choices.find(e => e.shared) ?? choices[0]);
  let view = $state<ReadState>(untrack(() => initialView));
  let detail = $state<ReadState>(untrack(() => initialDetail));
  let selection = $state<Selection | null>(untrack(() => initialSelection));
  let before = $state<RevisionSummary | null>(untrack(() => initialSelection?.kind === 'diff' ? initialSelection.before : null));
  let after = $state<RevisionSummary | null>(untrack(() => initialSelection?.kind === 'diff' ? initialSelection.after : null));
  let diffOffsets = $state<number[]>([]);
  let packageOffsets = $state<number[]>([]);
  let pairKey = $state(untrack(() => JSON.stringify([projectId, project.default_environment_id])));
  const pairInScope = $derived(pairKey === JSON.stringify([projectId, selected?.scope.environmentId]));
  let mounted = $state(false), visible = $state(false), blocked = $state(false);
  let overviewOwner = $state<ProvisionReader | null>(null), detailOwner = $state<ProvisionReader | null>(null);
  const overview = $derived(view.data?.project_id === projectId && view.data.environment_id === selected?.scope.environmentId ? view.data as ProvisionObservation : null);
  const selectionKey = $derived(JSON.stringify([projectId, selected?.scope.environmentId, selection]));
  let detailKey = $state(untrack(() => JSON.stringify([projectId, project.default_environment_id, initialSelection])));
  const selectedData = $derived(detailKey === selectionKey && detail.data?.project_id === projectId && detail.data.environment_id === selected?.scope.environmentId ? detail.data : null);
  const selectedOperation = $derived(selection?.kind === 'operation' || (selection?.kind === 'files' && !selection.retained) ? selection.operation : null);
  const selectedHead = $derived(selectedOperation ? overview?.operations.find(o => o.provision_id === selectedOperation.provision_id) : undefined);
  const selectionAdvanced = $derived(view.phase === 'observed' && selectedOperation !== null && (!selectedHead || selectedHead.seq !== selectedOperation.seq));
  const operation = $derived(selection?.kind === 'operation' || selection?.kind === 'retained_operation' ? selectedData as OperationDetail | null : null);
  const files = $derived(selection?.kind === 'files' ? selectedData as FilePage | null : null);
  const history = $derived(selection?.kind === 'history' ? selectedData as HistoryPage | null : null);
  const probes = $derived(selection?.kind === 'probes' ? selectedData as ProbePage | null : null);
  const comparison = $derived(selection?.kind === 'diff' ? selectedData as LockfileDiffPage | null : null);
  const packages = $derived(selection?.kind === 'packages' ? selectedData as RevisionPackagePage | null : null);
  const busy = $derived(view.busy || detail.busy);
  const signedOut = $derived(blocked || view.phase === 'signed_out' || detail.phase === 'signed_out');
  function select(value: Selection): void { if (!busy && !signedOut) selection = value; }
  function chooseRevision(side: 'before' | 'after', value: RevisionSummary): void {
    if (busy || signedOut) return;
    if (!pairInScope) before = after = null;
    pairKey = JSON.stringify([projectId, selected?.scope.environmentId]);
    if (side === 'before') before = value; else after = value;
    diffOffsets = [];
  }
  function compare(): void {if (pairInScope && before && after && !busy && !signedOut) {diffOffsets = []; select({kind: 'diff', before, after, offset: 0});}}
  function nextComparison(): void {
    if (busy || signedOut || selection?.kind !== 'diff' || comparison?.status !== 'complete' || comparison.next_offset === null) return;
    diffOffsets = [...diffOffsets, comparison.offset];
    select({...selection, offset: comparison.next_offset, expected: diffIdentity(comparison), previousKey: differenceKey(comparison.differences.at(-1)!)});
  }
  function previousComparison(): void {
    if (busy || signedOut || selection?.kind !== 'diff' || comparison?.status !== 'complete' || !diffOffsets.length) return;
    const offset = diffOffsets.at(-1)!; diffOffsets = diffOffsets.slice(0, -1);
    select({kind: 'diff', before: selection.before, after: selection.after, offset, expected: diffIdentity(comparison)});
  }
  function loadPackages(revision: RevisionSummary): void {if (!busy && !signedOut) {packageOffsets = []; select({kind: 'packages', revision, offset: 0});}}
  function nextPackages(): void {
    if (busy || signedOut || selection?.kind !== 'packages' || packages?.status !== 'complete' || packages.next_offset === null) return;
    packageOffsets = [...packageOffsets, packages.offset];
    select({...selection, offset: packages.next_offset, expected: packageIdentity(packages), previousKey: packageKey(packages.packages.at(-1)!)});
  }
  function previousPackages(): void {
    if (busy || signedOut || selection?.kind !== 'packages' || packages?.status !== 'complete' || !packageOffsets.length) return;
    const offset = packageOffsets.at(-1)!; packageOffsets = packageOffsets.slice(0, -1);
    select({kind: 'packages', revision: selection.revision, offset, expected: packageIdentity(packages)});
  }
  function holdReason(reason: string): string {return {lockfile_unrecorded: 'No lockfile was recorded', blob_missing: 'The recorded lockfile is unavailable or evicted', unsupported_format: 'The retained format is unsupported', skipped_entries: 'The retained lockfile omitted package entries', input_limit: 'The selected input exceeds this reader’s limits'}[reason] ?? 'Unknown input';}
  function environment(value: string): void { if (value !== selected?.scope.environmentId) { selectedId = value; selection = null; } }
  function phaseLabel(phase: ReadState['phase']): string {
    return {loading: 'Reading environment records…', observed: '', paused: 'Observations paused.', unavailable: 'Observation unavailable. Current state is unknown.', unsupported: 'This worker is unconfigured or does not support provisioning observations.', missing: 'This recorded environment or selected record is no longer available.', changed: 'This selection changed. Refresh the overview or history and select it again.', signed_out: 'Observations stopped for sign-out. Reload after signing in.'}[phase];
  }
  onMount(() => {
    mounted = true;
    const visibility = () => {visible = document.visibilityState === 'visible';};
    visibility(); document.addEventListener('visibilitychange', visibility);
    return () => {document.removeEventListener('visibilitychange', visibility);};
  });
  $effect(() => {
    if (!mounted || !selected) return;
    const scope = selected.scope;
    view = {phase: 'loading', data: null, busy: false}; selection = null; before = after = null; diffOffsets = []; packageOffsets = [];
    let disposed = false;
    const owner = new ProvisionReader(signal => readProvision(scope, {kind: 'summary'}, signal), value => {if (!disposed) {view = value; if (value.phase === 'signed_out') blocked = true;}});
    overviewOwner = owner;
    const off = onKernelLogout(() => owner.logout());
    return () => {disposed = true; owner.stop(); off(); overviewOwner = null;};
  });
  $effect(() => {overviewOwner?.setVisible(mounted && visible && !detail.busy && !blocked);});
  $effect(() => {
    if (!mounted || !selected || !selection) {detail = {phase: 'loading', data: null, busy: false}; return;}
    const scope = selected.scope, requested = selection;
    detailKey = selectionKey;
    detail = {phase: 'loading', data: null, busy: false};
    let disposed = false;
    const owner = new ProvisionReader(signal => readProvision(scope, requested, signal), value => {if (!disposed) {detail = value; if (value.phase === 'signed_out') blocked = true; if (requested.kind === 'diff' && value.phase === 'changed') {before = after = null; diffOffsets = [];} if (requested.kind === 'packages' && value.phase === 'changed') packageOffsets = []; }}, false, requested.kind === 'diff' ? DIFF_DEADLINE_MS : requested.kind === 'packages' ? PACKAGES_DEADLINE_MS : PROVISION_DEADLINE_MS, requested.kind === 'diff' || requested.kind === 'packages');
    detailOwner = owner;
    const off = onKernelLogout(() => owner.logout());
    return () => {disposed = true; owner.stop(); off(); detailOwner = null;};
  });
  $effect(() => {detailOwner?.setVisible(mounted && visible && !blocked);});
  $effect(() => {if (blocked) {overviewOwner?.logout(); detailOwner?.logout();}});
</script>

<section aria-label="Project environments" class="environment-panel">
  <h2>Environments</h2>
  <p class="muted">Package observations for the whole project environment, including work from its sessions. Viewing records does not start a stopped environment.</p>
  {#if choices.length === 0}
    <p role="status">No complete canonical environment selection is available.</p>
  {:else if selected}
    <label>Environment
      <select value={selected.scope.environmentId} onchange={e => environment(e.currentTarget.value)} disabled={signedOut}>
        {#each choices as choice (choice.scope.environmentId)}
          <option value={choice.scope.environmentId}>{choice.shared ? 'Shared project' : 'Dedicated'} · {choice.scope.environmentId}</option>
        {/each}
      </select>
    </label>
    <p class="muted">{selected.shared ? 'Shared project environment' : 'Dedicated environment'} · sessions: {selected.sessions.join(', ') || 'none recorded'}.</p>
    {#if phaseLabel(view.phase)}<p role="status">{phaseLabel(view.phase)}</p>{/if}
    {#if overview}
      {#if view.phase !== 'observed'}<p><strong>Previous observation; current state is unknown.</strong></p>{/if}
      <p class="muted">Observed <time datetime={overview.observed_at}>{formatTime(overview.observed_at)}</time>. Receipt time does not promise an atomic or current guest state.</p>
      <div class="status-grid">
        <div><span class="muted">Package admission</span><strong>{overview.gate.recorded ? overview.gate.mode : 'not recorded'}</strong></div>
        <div><span class="muted">Package revision</span><strong>{overview.current_revision ?? 'unknown'}</strong></div>
        <div><span class="muted">VM state</span><strong>not observed</strong></div>
        <div><span class="muted">Pending operations</span><strong>{overview.queue_total}</strong></div>
      </div>
      {#if overview.gate.mode === 'needs_recovery'}<p class="notice">New ordinary work is refused pending environment recovery. Viewing this panel grants no recovery action.</p>{/if}
      {#if overview.gate.mode === 'hold'}<p class="notice">Package admission is held{overview.gate.operation ? ` by ${overview.gate.operation}` : ''}.</p>{/if}
      {#if overview.gate.recorded}<p class="muted">Admission generation {overview.gate.generation}; recorded package revision {overview.gate.revision ?? 'unknown'}.</p>{/if}
      <p class="muted">Package admission is separate from VM state. A ready package gate does not mean the VM is running.</p>
      <div class="heading"><h3>Pending operations</h3><button disabled={busy || signedOut} onclick={() => select({kind: 'history', after: -1, target: null})}>Revision history</button></div>
      {#if overview.queue_total === 0}<p>No pending provisioning operations were recorded.</p>{:else}
        <div class="table-scroll"><table>
          <thead><tr><th>Operation</th><th>Session</th><th>Stage / state</th><th>Outcome</th><th>Recorded details</th></tr></thead>
          <tbody>{#each overview.operations as item (item.provision_id)}
            <tr><td><code>{item.provision_id}</code><br/><small>record {item.seq}</small></td><td>{item.session_id}</td>
              <td>{item.stage ?? 'not recorded'} / {item.state}</td><td>{item.outcome ?? 'not finished'}{#if item.reason}<br/><small>{item.reason}</small>{/if}</td>
              <td><button disabled={busy || signedOut} onclick={() => select({kind: 'operation', operation: item})}>Request</button>
                <button disabled={busy || signedOut} onclick={() => select({kind: 'files', operation: item, offset: 0})}>{item.files_total} resolved files</button>
                {#if item.permission_request}<p>Permission request {item.permission_request}{item.state === 'awaiting_permission' ? '; waiting for an operator answer.' : '; recorded reference, current decision not shown.'}</p>{/if}
              </td></tr>
          {/each}</tbody>
        </table></div>
      {/if}
    {/if}
    <section aria-label="Compare retained lockfiles" class="comparison-picker">
      <h3>Compare retained lockfiles</h3>
      <p class="muted">Choose each side from revision history, then compare the exact retained records. The same revision may be selected twice. No guest or install is started.</p>
      <div class="status-grid"><div><span>Before</span><strong>{pairInScope && before ? `Revision ${before.revision}` : 'Choose from history'}</strong>{#if pairInScope && before}<code class="digest">{before.record_sha256}</code>{/if}</div>
        <div><span>After</span><strong>{pairInScope && after ? `Revision ${after.revision}` : 'Choose from history'}</strong>{#if pairInScope && after}<code class="digest">{after.record_sha256}</code>{/if}</div></div>
      <div class="page-controls"><button disabled={busy || signedOut} onclick={() => select({kind: 'history', after: -1, target: null})}>Choose revisions</button>
        <button disabled={busy || signedOut || !pairInScope || !before || !after} onclick={compare}>Compare selected lockfiles</button></div>
    </section>
    {#if selection}
      <section aria-label="Selected environment record" class="selected-record">
        <div class="heading"><h3>Selected record</h3><button onclick={() => {selection = null;}}>Close detail</button></div>
        {#if phaseLabel(detail.phase)}<p role="status">{phaseLabel(detail.phase)}</p>{/if}
        {#if selectionAdvanced}<p><strong>The operation advanced or left the pending queue. This selected record is historical; choose a current queue entry to read a new sequence.</strong></p>{/if}
        {#if selectedData && detail.phase !== 'observed'}<p><strong>Previous selected record; current contents are unknown.</strong></p>{/if}
        {#if selectedData}<p class="muted">Observed {formatTime(selectedData.observed_at)}.</p>{/if}
        {#if operation}
          {#if selection.kind === 'retained_operation'}<p class="notice">Current retained operation state, selected from revision history. This does not reproduce its state at that revision.</p>{/if}
          <h4>Request <code>{operation.summary.provision_id}</code></h4>
          <p>Sequence {operation.summary.seq}; session {operation.summary.session_id}; {operation.summary.state}; outcome {operation.summary.outcome ?? 'not finished'}.</p>
          <dl class="facts"><dt>Purpose</dt><dd class="record-text">{operation.request.purpose}</dd><dt>Requested</dt><dd>{operation.request.capability ?? [...operation.request.conda, ...operation.request.pip, ...operation.request.system].join(', ')}</dd>
            <dt>Stage / reason</dt><dd>{operation.summary.stage ?? 'not recorded'} / {operation.summary.reason ?? 'none recorded'}</dd><dt>Revisions</dt><dd>planned {operation.summary.planned_revision ?? 'unknown'}; before {operation.summary.revision_before ?? 'unknown'}; after {operation.summary.revision_after ?? 'unknown'}</dd>
            <dt>Policy</dt><dd><code>{operation.policy ?? 'not recorded'}</code></dd><dt>Error</dt><dd>{operation.error_present ? operation.error_kind ?? 'recorded; kind unknown' : 'none recorded'}</dd></dl>
          <p class="muted">Submitted {formatTime(operation.summary.submitted_at)}; started {operation.summary.started_at ? formatTime(operation.summary.started_at) : 'not recorded'}; ended {operation.summary.ended_at ? formatTime(operation.summary.ended_at) : 'not recorded'}.</p>
          <button disabled={busy || signedOut || selectionAdvanced} onclick={() => select({kind: 'files', operation: operation.summary, offset: 0, ...(selection?.kind === 'retained_operation' ? {retained: true as const} : {})})}>{operation.summary.files_total} resolved files at sequence {operation.summary.seq}</button>
          {#if operation.request.pip_reason}<p class="record-text">pip fallback reason: {operation.request.pip_reason}</p>{/if}
          {#if operation.next_step}<p class="record-text">Recorded next step: {operation.next_step}</p>{/if}
          <p class="muted">Request text and next steps are recorded data. This viewer does not execute them or answer permissions. Notes, raw errors and execution details are outside this view.</p>
        {:else if files}
          <h4>Resolved files</h4>
          {#if selection.kind === 'files' && selection.retained}<p class="notice">Files pinned to the sequence of an explicitly loaded current retained operation, rather than its revision-era state.</p>{/if}
          <p>Operation <code>{files.provision_id}</code>, record {files.seq}. {files.total === 0 ? 'No resolved files recorded.' : `Showing files ${files.offset + 1}–${files.offset + files.files.length} of ${files.total}.`}</p>
          {#if files.next_offset !== null || files.offset > 0}<p class="notice">This is a page of the file set. Review all pages before treating the set as complete.</p>{/if}
          {#each files.files as file, index (`${files.offset + index}`)}
            <article><h5>{file.name} {file.version} <small>({file.ecosystem})</small>{#if file.agpl}<span class="license-flag">AGPL flagged</span>{/if}</h5>
              <p>Declared license: <strong>{file.license ?? 'unknown'}</strong>; policy action: {file.action}{file.rule ? ` (${file.rule})` : ''}.</p>
              <dl class="facts"><dt>File / build</dt><dd><code>{file.file}</code> / {file.build ?? 'not recorded'}</dd><dt>Channel / platform</dt><dd>{file.channel ?? 'not recorded'} / {file.subdir ?? 'not recorded'}</dd><dt>SHA-256</dt><dd><code>{file.sha256 ?? 'not recorded'}</code></dd></dl>
              {#if file.url}<p class="record-text">Recorded source: {file.url}</p>{/if}
            </article>
          {/each}
          <p class="muted">Declared licenses and decisions are observations, not license certification or download approval. An unknown license is not a permissive license.</p>
          {#if selection.kind === 'files'}
            <div class="page-controls"><button disabled={busy || signedOut || selectionAdvanced || files.offset === 0} onclick={() => {if (selection?.kind === 'files') select({...selection, offset: Math.max(0, files.offset - 8)});}}>Previous files</button>
              <button disabled={busy || signedOut || selectionAdvanced || files.next_offset === null} onclick={() => {if (selection?.kind === 'files' && files.next_offset !== null) select({...selection, offset: files.next_offset});}}>Next files</button></div>
          {/if}
        {:else if history}
          <h4>Revision history</h4>
          <p>Retained records: {history.retained_total}; first retained: {history.first_retained ?? 'none'}; selected window ends at revision {history.target ?? 'none'}.</p>
          <p class="muted">Retention is limited. Missing lockfiles may have been evicted or never recorded. Select explicit before and after records for a bounded lockfile comparison; revisions are not restored.</p>
          {#if history.revisions.length === 0}<p>No retained revisions in this selected window.</p>{:else}
            <div class="table-scroll"><table><thead><tr><th>Revision</th><th>Change</th><th>Lockfile</th><th>Probes</th><th>Compare</th></tr></thead><tbody>
              {#each history.revisions as item (item.revision)}<tr><td>{item.revision}<br/><small>{formatTime(item.recorded_at)}</small></td><td>{item.managed === null ? 'origin unknown' : item.managed ? 'managed' : 'unmanaged'}<br/><small>{item.operation ?? item.changed_by ?? 'no producing operation recorded'}</small></td>
                <td>{item.lockfile ? `${item.lockfile.conda_packages} Conda / ${item.lockfile.pip_distributions} pip` : 'unavailable'}{#if item.lockfile}<br/><code class="digest">{item.lockfile.sha256}</code>{/if}<br/><button disabled={busy || signedOut} onclick={() => loadPackages(item)}>Recorded packages and licenses</button></td>
                <td><button disabled={busy || signedOut} onclick={() => select({kind: 'probes', revision: item, offset: 0})}>{item.probes_total} probe records</button>{#if item.operation}<br/><button disabled={busy || signedOut} onclick={() => {if (item.operation) select({kind: 'retained_operation', provisionId: item.operation});}}>Load current retained operation</button>{/if}</td>
                <td><button disabled={busy || signedOut} onclick={() => chooseRevision('before', item)}>Use as before</button><button disabled={busy || signedOut} onclick={() => chooseRevision('after', item)}>Use as after</button></td></tr>{/each}
            </tbody></table></div>
          {/if}
          <div class="page-controls"><button disabled={busy || signedOut} onclick={() => select({kind: 'history', after: -1, target: null})}>Refresh history window</button>
            <button disabled={busy || signedOut || history.next_after === null} onclick={() => {if (history.next_after !== null) select({kind: 'history', after: history.next_after, target: history.target});}}>Next revisions</button></div>
        {:else if packages}
          <h4>Recorded packages and licenses · revision {packages.selection.revision}</h4>
          <p class="muted">Dated retained package records, including unchanged packages. The backend verifies the selected raw record and lockfile bytes; this browser does not independently hash them. Current environment changes do not select a newer revision.</p>
          <details><summary>Selected source proof</summary><pre>{JSON.stringify({selection: packages.selection, verified_header: packages.header}, null, 2)}</pre></details>
          {#if packages.status === 'held'}
            <p class="notice"><strong>Recorded package inventory held; packages and licenses are unknown.</strong> An unavailable or incomplete input is not an empty inventory.</p>
            <ul>{#each packages.holds as hold}<li>{holdReason(hold)}.</li>{/each}</ul>
          {:else}
            <p>{packages.total === 0 ? 'No package entries in this validated retained lockfile.' : `Showing recorded packages ${packages.offset + 1}–${packages.offset + packages.packages.length} of ${packages.total}. This page is a subset of the recorded inventory.`}</p>
            {#each packages.packages as row (packageKey(row))}
              <article><h5>{row.package.name} {row.package.version} <small>({row.ecosystem}{row.location ? `, ${row.location}` : ''})</small>{#if row.license.agpl}<span class="license-flag">AGPL flagged</span>{/if}</h5>
                <p>Declared license: <strong>{row.license.declared ?? 'unknown'}</strong>; AGPL status: {row.license.agpl === null ? 'unknown' : row.license.agpl ? 'flagged' : 'not flagged'}.</p>
                <dl class="facts">{#each Object.entries(row.package) as [field, value]}<dt>{field}</dt><dd class="record-text">{value ?? 'unknown'}</dd>{/each}</dl>
              </article>
            {/each}
            <p class="muted">Conda and pip licenses are declared metadata with the existing AGPL heuristic. Missing declarations remain unknown. Recorded packages do not certify installed files, complete guest inventory, scientific correctness, licensing or permission.</p>
            <div class="page-controls"><button disabled={busy || signedOut || detail.phase !== 'observed' || !packageOffsets.length} onclick={previousPackages}>Previous packages</button>
              <button disabled={busy || signedOut || detail.phase !== 'observed' || packages.next_offset === null} onclick={nextPackages}>Next packages</button></div>
          {/if}
          {#if detail.phase === 'changed'}<p class="notice">Select the revision again from refreshed history. No newer source was substituted.</p>{/if}
          <button disabled={busy || signedOut} onclick={() => select({kind: 'history', after: -1, target: null})}>Back to revision history</button>
        {:else if comparison}
          <h4>Lockfile comparison · revision {comparison.before.revision} → {comparison.after.revision}</h4>
          <p class="muted">These are dated retained package records. Advancing the environment head does not select newer sources. Raw revision pins and lockfile byte hashes are verified by the backend, rather than independently hashed by this browser.</p>
          {#each [['Before', comparison.before, comparison.before_header], ['After', comparison.after, comparison.after_header]] as side}
            <details><summary>{side[0]} source proof</summary><pre>{JSON.stringify({selection: side[1], verified_header: side[2]}, null, 2)}</pre></details>
          {/each}
          {#if comparison.status === 'held'}
            <p class="notice"><strong>Comparison held; package differences are unknown.</strong> An unavailable or incomplete input is not an empty inventory.</p>
            <ul>{#each comparison.holds as hold}<li>{hold.side === 'before' ? 'Before' : 'After'}: {holdReason(hold.reason)}.</li>{/each}</ul>
          {:else}
            <p>{comparison.added} added · {comparison.removed} removed · {comparison.changed} changed · {comparison.total} total differences.</p>
            {#if comparison.header_changes.length}<p>Recorded header changes: {comparison.header_changes.join(', ')}.</p>{/if}
            {#if comparison.total === 0}<p>No package-entry differences in these two validated retained lockfiles.</p>{:else}<p>Showing differences {comparison.offset + 1}–{comparison.offset + comparison.differences.length} of {comparison.total}. This page is a subset of the comparison.</p>{/if}
            {#each comparison.differences as row (differenceKey(row))}
              <article><h5>{row.name} · {row.change} <small>({row.ecosystem}{row.location ? `, ${row.location}` : ''})</small></h5>
                <p>Changed fields: {row.changed_fields.join(', ')}.</p>
                <div class="comparison-sides">{#each [{label: 'Before', entry: row.before, license: row.before_license}, {label: 'After', entry: row.after, license: row.after_license}] as side}
                  <section aria-label={`${side.label} ${row.name}`}><h6>{side.label}</h6>
                    {#if side.entry}<p>Declared license: <strong>{side.license?.declared ?? 'unknown'}</strong>; AGPL status: {side.license?.agpl === null ? 'unknown' : side.license?.agpl ? 'flagged' : 'not flagged'}.</p>
                      <dl class="facts">{#each Object.entries(side.entry) as [field, value]}<dt>{field}</dt><dd class="record-text">{value ?? 'unknown'}</dd>{/each}</dl>
                    {:else}<p>No recorded entry on this side.</p>{/if}
                  </section>
                {/each}</div>
              </article>
            {/each}
            <p class="muted">Conda and pip licenses are declared metadata with the existing AGPL heuristic. Missing declarations remain unknown. Membership and metadata differences do not certify installed files, scientific correctness, licensing or permission.</p>
            <div class="page-controls"><button disabled={busy || signedOut || detail.phase !== 'observed' || !diffOffsets.length} onclick={previousComparison}>Previous differences</button>
              <button disabled={busy || signedOut || detail.phase !== 'observed' || comparison.next_offset === null} onclick={nextComparison}>Next differences</button></div>
          {/if}
          {#if detail.phase === 'changed'}<p class="notice">Select both revisions again from refreshed history. No newer source was substituted.</p>{/if}
        {:else if probes}
          <h4>Probes at revision {probes.revision}</h4><p>{probes.total === 0 ? 'No probe records retained at this revision.' : `Showing probes ${probes.offset + 1}–${probes.offset + probes.probes.length} of ${probes.total}.`}</p>
          {#each probes.probes as probe, index (`${probes.offset + index}`)}<article><h5>{probe.probe}: {probe.status}</h5><p>{probe.revision === null ? 'Revision attribution unknown. ' : ''}{probe.kind === 'functional' ? 'Functional probe' : 'Import/version check; does not prove functional capability'} · version {probe.version} · {probe.duration_seconds}s.</p><pre>{probe.output || '(no output recorded)'}</pre></article>{/each}
          {#if selection.kind === 'probes'}<div class="page-controls"><button disabled={busy || signedOut || probes.offset === 0} onclick={() => {if (selection?.kind === 'probes') select({...selection, offset: Math.max(0, probes.offset - 8)});}}>Previous probes</button>
            <button disabled={busy || signedOut || probes.next_offset === null} onclick={() => {if (selection?.kind === 'probes' && probes.next_offset !== null) select({...selection, offset: probes.next_offset});}}>Next probes</button></div>{/if}
          <button disabled={busy || signedOut} onclick={() => select({kind: 'history', after: -1, target: null})}>Back to revision history</button>
        {/if}
      </section>
    {/if}
  {/if}
</section>

<style>
  .environment-panel { margin-top: 2rem; }
  label {display: grid; gap: .4rem; max-width: 40rem; font-weight: 600;}
  select {max-width: 100%; padding: .6rem;}
  .status-grid {display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0;}
  .status-grid > div {display: grid; gap: .35rem; padding: .75rem 1rem; border: 1px solid var(--border, #bbb); border-radius: .4rem; min-width: 10rem;}
  .heading {display: flex; align-items: center; justify-content: space-between; gap: 1rem;}
  .selected-record {margin-top: 1rem; padding: 1rem; border: 1px solid var(--border, #bbb); border-radius: .4rem;}
  .comparison-picker {margin-top: 1rem; padding-top: .5rem; border-top: 1px solid var(--border, #bbb);}
  .comparison-sides {display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 20rem), 1fr)); gap: 1rem;}
  h6 {font-size: 1rem; margin: .5rem 0;}
  .table-scroll {overflow-x: auto;}
  article {padding: .7rem 0; border-bottom: 1px solid var(--border, #bbb);}
  h5 {font-size: 1rem; margin: .4rem 0;}
  .record-text, dd, code {overflow-wrap: anywhere;}
  .record-text {white-space: pre-wrap;}
  .digest {font-size: .75rem;}
  .license-flag {display: inline-block; margin-left: .6rem; padding: .1rem .4rem; border: 1px solid currentColor; border-radius: .3rem;}
  .notice {border-left: .25rem solid #a66b00; padding-left: .75rem;}
  .page-controls {display: flex; flex-wrap: wrap; gap: .6rem; margin-top: 1rem;}
  pre {white-space: pre-wrap; overflow-wrap: anywhere; max-height: 18rem; overflow: auto;}
</style>
