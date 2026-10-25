<!-- SPDX-License-Identifier: MIT -->
<!-- A service's Classification tab (docs/specs/workspace-browsers.md, CL-1 to CL-4): the
     classification in force, read-only, with each tool's pin state; the drafts beside it;
     and the latest drift with Re-accept. Viewing reads the worker's records only. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type {
    ClassificationView,
    ClassifiedTool,
    DraftListing,
    DriftAccepted,
  } from '../lib/api-types';
  import { MAX_REASON, ruleWords, significantWords } from '../lib/classifications';
  import { formatTime } from '../lib/format';
  import { guardedWrite } from '../lib/mutation';
  import { visible } from '../lib/text';
  import ClassificationDraft from './ClassificationDraft.svelte';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId, server }: { projectId: string; server: string } = $props();
  let view = $state<ClassificationView | null>(null);
  let error = $state<unknown>(null);
  let drafts = $state<DraftListing | null>(null);
  let draftError = $state<unknown>(null);
  let loading = $state(true);

  async function load() {
    const [project, name] = [projectId, server];
    loading = true;
    const [found, listed] = await Promise.allSettled([
      api.classification(project, name),
      api.classificationDrafts(project, name),
    ]);
    if (project !== projectId || name !== server) return; // another service was opened
    view = found.status === 'fulfilled' && Array.isArray(found.value?.tools) ? found.value : null;
    error = found.status === 'rejected' ? found.reason : null;
    drafts =
      listed.status === 'fulfilled' && Array.isArray(listed.value?.drafts) ? listed.value : null;
    draftError = listed.status === 'rejected' ? listed.reason : null;
    loading = false;
  }
  $effect(() => {
    void [projectId, server];
    view = null;
    drafts = null;
    reaccepted = null;
    void load();
  });

  const pinLabel: Record<ClassifiedTool['pin_state'], string> = {
    current: 'Current',
    changed: 'Changed',
    added: 'Added',
    removed: 'Removed',
    unpinned: 'Unpinned',
  };
  const pinTone: Record<ClassifiedTool['pin_state'], string> = {
    current: 'ok',
    changed: 'warn',
    added: 'warn',
    removed: 'failed',
    unpinned: '',
  };

  // Re-acceptance after drift (CL-4).
  let reason = $state('');
  let problem = $state<string | null>(null);
  let saving = $state(false);
  let writeError = $state<unknown>(null);
  let reaccepted = $state<DriftAccepted | null>(null);
  async function reaccept() {
    if (saving) return;
    if (!reason.trim()) {
      problem = 'A reason is required to re-accept.';
      return;
    }
    if (reason.length > MAX_REASON) {
      problem = `A reason is at most ${MAX_REASON} characters.`;
      return;
    }
    problem = null;
    writeError = null;
    saving = true;
    try {
      reaccepted = await guardedWrite(
        'classification-drift',
        `${projectId}:${server}`,
        { reason },
        (change) => api.acceptDrift(projectId, server, change),
      );
      reason = '';
      await load();
    } catch (caught) {
      writeError = caught;
    } finally {
      saving = false;
    }
  }
</script>

<div class="classification">
  <p class="muted">
    Read-only. Viewing reads the classification and the middleware's records; it calls no tool and
    grants nothing.
  </p>
  {#if error}<ErrorMessage {error} />{/if}
  {#if loading && view === null}<p class="muted">Loading…</p>{/if}
  {#if view}
    {@const source = view.source}
    <p class="source">
      {#if source.kind === 'catalog'}
        In force: checked-in file <code>{visible(source.name ?? view.server)}</code
        >{#if source.pinned}, pinned {source.pinned}{/if}.
      {:else}
        In force: project revision <code>{(source.revision ?? '').slice(0, 12)}</code
        >{#if source.accepted_by}, accepted by {visible(
            source.accepted_by,
          )}{/if}{#if source.accepted_at}
          at {formatTime(source.accepted_at)}{/if}{#if source.reason}: “{visible(
            source.reason,
          )}”{/if}.
      {/if}
      <span class="muted">Digest <code>{view.digest}</code></span>
      {#if source.kind === 'project' && source.revision}
        <a
          class="export"
          href={api.classificationExportUrl(projectId, view.server, source.revision)}
          download>Export YAML</a
        >
      {/if}
    </p>
    <div class="table-scroll">
      <table class="tools">
        <thead>
          <tr>
            <th scope="col">Tool</th>
            <th scope="col">Class</th>
            <th scope="col">Reason</th>
            <th scope="col">Rules</th>
            <th scope="col">Significant</th>
            <th scope="col">Pin</th>
          </tr>
        </thead>
        <tbody>
          {#each view.tools as tool (tool.name)}
            <tr data-tool={tool.name}>
              <th scope="row"><code>{visible(tool.name)}</code></th>
              <td>
                <span class="pill effect">{tool.effect}</span>
                {#if !tool.mentioned}
                  <div class="unmentioned">
                    Not in the classification: its hints give {tool.effect}{#if tool.escalates}<span
                        class="pill warn escalates">Escalates</span
                      >{/if}
                  </div>
                {/if}
              </td>
              <td>{tool.mentioned ? visible(tool.reason ?? '') : ''}</td>
              <td>
                {#each tool.rules as rule}<div class="rule">
                    {visible(ruleWords(rule))}
                  </div>{:else}<span class="muted">None</span>{/each}
              </td>
              <td>{visible(significantWords(tool.significant)) || '—'}</td>
              <td
                ><span class="pill pin {pinTone[tool.pin_state]}">{pinLabel[tool.pin_state]}</span
                ></td
              >
            </tr>
          {:else}<tr><td colspan="6" class="muted">The classification names no tools.</td></tr
            >{/each}
        </tbody>
      </table>
    </div>

    <section class="drift" aria-label="Drift">
      <h3>Drift</h3>
      {#if view.drift}
        {@const drift = view.drift}
        <p>
          The tool list differed from the pins at {formatTime(drift.at)} (pin
          <code>{drift.pin}</code>).
          {#if drift.held}<strong>The middleware holds this server's non-read calls</strong>: they
            ask for approval until the tool list is re-accepted.{:else}No calls are held.{/if}
        </p>
        <dl class="facts">
          <dt>Added</dt>
          <dd>
            {#each drift.added as item}<div>
                <code>{visible(item.name)}</code> <small class="muted">{item.sha256}</small>
              </div>{:else}None{/each}
          </dd>
          <dt>Removed</dt>
          <dd>
            {#each drift.removed as name}<div><code>{visible(name)}</code></div>{:else}None{/each}
          </dd>
          <dt>Changed</dt>
          <dd>
            {#each drift.changed as item}<div>
                <code>{visible(item.name)}</code> <small class="muted">{item.sha256}</small>
              </div>{:else}None{/each}
          </dd>
        </dl>
        <p class="muted">
          Re-accepting keeps a changed tool's class, leaves an added tool unclassified until a
          classification names it, and lists a removed one as gone.
        </p>
        <label class="reason"
          >Reason for re-accepting (required) <textarea
            bind:value={reason}
            maxlength={MAX_REASON}
            rows="2"></textarea></label
        >
        <button
          type="button"
          class="primary-button"
          disabled={saving}
          onclick={() => void reaccept()}>{saving ? 'Re-accepting…' : 'Re-accept'}</button
        >
        {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
        {#if writeError}<ErrorMessage error={writeError} />{/if}
      {:else}<p class="muted">No drift recorded for this server.</p>{/if}
      {#if reaccepted}<p role="status">
          Re-accepted at {formatTime(reaccepted.accepted_at)}.
        </p>{/if}
    </section>

    <section class="drafts" aria-label="Drafts">
      <h3>Drafts</h3>
      {#if draftError}<ErrorMessage error={draftError} />{/if}
      {#if drafts}
        {#each drafts.drafts as draft (draft.draft)}
          <ClassificationDraft
            {projectId}
            {server}
            {draft}
            current={view.tools}
            onaccepted={load}
          />
        {:else}<p class="muted">No drafts for this server.</p>{/each}
      {/if}
    </section>
  {/if}
</div>

<style>
  .table-scroll {
    overflow-x: auto;
  }
  .tools {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
  }
  .tools th,
  .tools td {
    text-align: left;
    vertical-align: top;
    padding: 0.4rem;
    border-bottom: 1px solid var(--border, #ccc);
  }
  .rule,
  .unmentioned {
    font-size: 0.85rem;
  }
  .reason {
    display: grid;
    gap: 0.2rem;
    margin: 0.5rem 0;
  }
  .export {
    margin-left: 0.5rem;
  }
  .source code {
    overflow-wrap: anywhere;
  }
</style>
