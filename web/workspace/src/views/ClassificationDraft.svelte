<!-- SPDX-License-Identifier: MIT -->
<!-- One classification draft beside the classification in force, tool by tool
     (docs/specs/workspace-browsers.md, CL-2 and CL-3): the model's proposal, framed as
     such, with an editor per tool and an acceptance that confirms every widening. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type {
    ArgumentRule,
    ClassificationAccepted,
    ClassificationDraft,
    ClassifiedTool,
    EffectClass,
    SignificantKind,
  } from '../lib/classificationTypes';
  import {
    changed,
    EFFECT_CLASSES,
    editable,
    MAX_REASON,
    problems,
    rewriteYaml,
    ruleChanges,
    ruleWords,
    SIGNIFICANT_KINDS,
    significantWords,
    widens,
    type EditedTool,
  } from '../lib/classifications';
  import { formatTime } from '../lib/format';
  import { guardedWrite } from '../lib/mutation';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    server,
    draft,
    current,
    onaccepted,
  }: {
    projectId: string;
    server: string;
    draft: ClassificationDraft;
    /** The classification in force's tools, by name. */
    current: ClassifiedTool[];
    onaccepted: () => Promise<void>;
  } = $props();

  let edits = $state<Record<string, EditedTool>>(
    untrack(() => Object.fromEntries(draft.tools.map((tool) => [tool.name, editable(tool)]))),
  );
  let editing = $state<string | null>(null);
  const currentByName = $derived(new Map(current.map((tool) => [tool.name, tool])));
  const draftByName = $derived(new Map(draft.tools.map((tool) => [tool.name, tool])));
  const names = $derived([
    ...draft.tools.map((tool) => tool.name),
    ...current.map((tool) => tool.name).filter((name) => !draftByName.has(name)),
  ]);
  const edited = $derived(
    draft.tools.filter((tool) => edits[tool.name] && changed(edits[tool.name]!, tool)),
  );
  const widenings = $derived(
    draft.tools
      .filter(
        (tool) => edits[tool.name] && widens(edits[tool.name]!, tool, currentByName.get(tool.name)),
      )
      .map((tool) => tool.name),
  );
  const invalid = $derived(
    draft.tools.flatMap((tool) =>
      (edits[tool.name] ? problems(edits[tool.name]!) : []).map((text) => `${tool.name}: ${text}`),
    ),
  );

  // The rule being added in the open editor.
  let ruleArgument = $state('');
  let ruleCondition = $state<'present' | 'equals'>('present');
  let ruleValue = $state('');
  let ruleEffect = $state<EffectClass>('write');
  let significantKind = $state<SignificantKind>('url');
  let significantPath = $state('');

  function value(text: string): boolean | number | string {
    if (text === 'true' || text === 'false') return text === 'true';
    if (/^-?(?:0|[1-9][0-9]{0,15})$/.test(text)) return Number(text);
    return text;
  }
  function addRule(name: string) {
    const tool = edits[name];
    if (!tool || !ruleArgument.trim()) return;
    const rule: ArgumentRule = {
      argument: ruleArgument.trim(),
      present: ruleCondition === 'present' ? true : null,
      equals: ruleCondition === 'equals' ? value(ruleValue) : null,
      effect: ruleEffect,
    };
    update(name, { rules: [...tool.rules, rule] });
    ruleArgument = '';
    ruleValue = '';
  }
  function addSignificant(name: string) {
    const tool = edits[name];
    if (!tool || !significantPath.trim()) return;
    update(name, {
      significant: [...tool.significant, { kind: significantKind, path: significantPath.trim() }],
    });
    significantPath = '';
  }
  function update(name: string, patch: Partial<EditedTool>) {
    const tool = edits[name];
    if (tool) edits[name] = { ...tool, ...patch };
  }
  function reset(name: string) {
    const tool = draftByName.get(name);
    if (tool) edits[name] = editable(tool);
  }

  // Acceptance: the confirmation lists every widening and the rules that change.
  let confirming = $state(false);
  let reason = $state('');
  let problem = $state<string | null>(null);
  let yaml = $state('');
  let saving = $state(false);
  let error = $state<unknown>(null);
  let accepted = $state<ClassificationAccepted | null>(null);
  const changes = $derived(
    draft.tools
      .map((tool) => {
        const mine = edits[tool.name] ?? editable(tool);
        const before = currentByName.get(tool.name);
        return {
          name: tool.name,
          from: before?.effect ?? tool.current_effect ?? null,
          to: mine.effect,
          ...ruleChanges(mine, before),
        };
      })
      .filter((item) => item.from !== item.to || item.added.length > 0 || item.removed.length > 0),
  );

  function open() {
    problem = null;
    error = null;
    if (invalid.length) {
      problem = 'Fix the edits first.';
      return;
    }
    const written = rewriteYaml(
      draft.yaml,
      new Map(edited.map((tool) => [tool.name, $state.snapshot(edits[tool.name]!)])),
    );
    if ('error' in written) {
      problem = written.error;
      return;
    }
    yaml = written.yaml;
    confirming = true;
  }
  async function confirm() {
    if (saving) return;
    if (!reason.trim()) {
      problem = 'A reason is required.';
      return;
    }
    if (reason.length > MAX_REASON) {
      problem = `A reason is at most ${MAX_REASON} characters.`;
      return;
    }
    problem = null;
    saving = true;
    error = null;
    try {
      accepted = await guardedWrite(
        'classification-accept',
        `${projectId}:${server}:${draft.draft}`,
        { yaml, reason },
        (change) => api.acceptClassificationDraft(projectId, server, draft.draft, change),
      );
      confirming = false;
      await onaccepted();
    } catch (caught) {
      error = caught;
    } finally {
      saving = false;
    }
  }
</script>

<section class="classification-draft" aria-label={`Draft ${draft.draft.slice(0, 12)}`}>
  <header>
    <strong>Draft <code>{draft.draft.slice(0, 12)}</code></strong>
    <span class="muted">created {formatTime(draft.created_at)} · model {visible(draft.model)}</span>
  </header>
  <p class="framing" role="note">
    Proposed by a model from the server's tool list. Classes, reasons and rules below are the
    model's text, not a decision: review each one before accepting.
  </p>
  <div class="table-scroll">
    <table class="compare">
      <thead>
        <tr>
          <th scope="col">Tool</th>
          <th scope="col">In force</th>
          <th scope="col">Proposed (model's text{edited.length ? ', edited' : ''})</th>
          <th scope="col"><span class="sr-only">Edit</span></th>
        </tr>
      </thead>
      <tbody>
        {#each names as name (name)}
          {@const before = currentByName.get(name)}
          {@const proposed = draftByName.get(name)}
          {@const mine = edits[name]}
          <tr class="tool-row" data-tool={name}>
            <th scope="row"><code>{visible(name)}</code></th>
            <td>
              {#if before}
                <span class="pill effect">{before.effect}</span>
                <p>{visible(before.reason)}</p>
                {#each before.rules as rule}<div class="rule">{ruleWords(rule)}</div>{/each}
              {:else}<span class="muted">Not classified now</span>{/if}
            </td>
            <td>
              {#if proposed && mine}
                <span class="pill effect">{mine.effect}</span>
                {#if widens(mine, proposed, before)}<span class="pill failed widening">Widens</span
                  >{/if}
                {#if changed(mine, proposed)}<span class="pill info">Edited</span>{/if}
                <p>{visible(mine.reason)}</p>
                {#each mine.rules as rule}<div class="rule">{visible(ruleWords(rule))}</div>{/each}
                {#if mine.significant.length}<div class="muted">
                    Significant: {visible(
                      significantWords(
                        Object.fromEntries(mine.significant.map((item) => [item.kind, item.path])),
                      ),
                    )}
                  </div>{/if}
              {:else}<span class="muted">Not in the draft</span>{/if}
            </td>
            <td>
              {#if proposed}<button
                  type="button"
                  aria-expanded={editing === name}
                  onclick={() => (editing = editing === name ? null : name)}
                  >{editing === name ? 'Close' : 'Edit'}</button
                >{/if}
            </td>
          </tr>
          {#if editing === name && mine}
            <tr class="editor-row">
              <td colspan="4">
                <div class="editor" aria-label={`Edit ${name}`}>
                  <label
                    >Class <select
                      value={mine.effect}
                      onchange={(event) =>
                        update(name, { effect: event.currentTarget.value as EffectClass })}
                      >{#each EFFECT_CLASSES as effect}<option value={effect}>{effect}</option
                        >{/each}</select
                    ></label
                  >
                  <label class="wide"
                    >Reason <input
                      type="text"
                      maxlength="200"
                      value={mine.reason}
                      oninput={(event) => update(name, { reason: event.currentTarget.value })}
                    /></label
                  >
                  <fieldset>
                    <legend>Argument rules</legend>
                    {#each mine.rules as rule, index (index)}
                      <div class="rule">
                        {visible(ruleWords(rule))}
                        <button
                          type="button"
                          aria-label={`Remove rule ${ruleWords(rule)}`}
                          onclick={() =>
                            update(name, { rules: mine.rules.filter((_, at) => at !== index) })}
                          >Remove</button
                        >
                      </div>
                    {:else}<p class="muted">No rules: every call has the class above.</p>{/each}
                    <div class="add">
                      <label>Argument <input type="text" bind:value={ruleArgument} /></label>
                      <label
                        >When <select
                          value={ruleCondition}
                          onchange={(event) =>
                            (ruleCondition =
                              event.currentTarget.value === 'equals' ? 'equals' : 'present')}
                          ><option value="present">present</option><option value="equals"
                            >equals</option
                          ></select
                        ></label
                      >
                      {#if ruleCondition === 'equals'}<label
                          >Value <input type="text" bind:value={ruleValue} /></label
                        >{/if}
                      <label
                        >Class <select bind:value={ruleEffect}
                          >{#each EFFECT_CLASSES as effect}<option value={effect}>{effect}</option
                            >{/each}</select
                        ></label
                      >
                      <button type="button" onclick={() => addRule(name)}>Add rule</button>
                    </div>
                  </fieldset>
                  <fieldset>
                    <legend>Significant arguments</legend>
                    {#each mine.significant as item, index (index)}
                      <div class="rule">
                        {item.kind}: {visible(item.path)}
                        <button
                          type="button"
                          aria-label={`Remove significant ${item.kind}`}
                          onclick={() =>
                            update(name, {
                              significant: mine.significant.filter((_, at) => at !== index),
                            })}>Remove</button
                        >
                      </div>
                    {/each}
                    <div class="add">
                      <label
                        >Kind <select bind:value={significantKind}
                          >{#each SIGNIFICANT_KINDS as kind}<option value={kind}>{kind}</option
                            >{/each}</select
                        ></label
                      >
                      <label>Argument <input type="text" bind:value={significantPath} /></label>
                      <button type="button" onclick={() => addSignificant(name)}
                        >Add significant argument</button
                      >
                    </div>
                  </fieldset>
                  {#each problems(mine) as text}<p class="problem">{text}</p>{/each}
                  <button type="button" onclick={() => reset(name)}>Undo edits to this tool</button>
                </div>
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>
  </div>
  {#if accepted}
    <p role="status">
      Accepted as revision {accepted.revision} at {formatTime(accepted.accepted_at)}{accepted
        .widenings.length
        ? `; widenings: ${accepted.widenings.join(', ')}`
        : ''}.
    </p>
  {:else if !confirming}
    <button type="button" class="primary-button" onclick={open}>Accept…</button>
  {/if}
  {#if confirming}
    <div class="confirm" role="dialog" aria-label="Confirm acceptance">
      <h4>Accept this draft{edited.length ? ` with ${edited.length} edited tools` : ''}?</h4>
      <p>
        It becomes this project's classification of {visible(server)}, superseding the one in force.
      </p>
      <h5>Widenings</h5>
      {#if widenings.length}
        <p class="problem">
          Calls of these tools that ask for approval today would pass without it:
        </p>
        <ul class="widenings">
          {#each widenings as name}<li><code>{visible(name)}</code></li>{/each}
        </ul>
      {:else}<p class="muted">None estimated; the worker lists any it finds.</p>{/if}
      <h5>Changes from the classification in force</h5>
      {#if changes.length}
        <ul class="changes">
          {#each changes as item (item.name)}
            <li>
              <code>{visible(item.name)}</code>
              {#if item.from !== item.to}: {item.from ?? 'not classified'} → {item.to}{/if}
              {#each item.added as words}<div class="rule">+ {visible(words)}</div>{/each}
              {#each item.removed as words}<div class="rule">− {visible(words)}</div>{/each}
            </li>
          {/each}
        </ul>
      {:else}<p class="muted">No class or rule changes.</p>{/if}
      <label class="wide"
        >Reason (required) <textarea bind:value={reason} maxlength={MAX_REASON} rows="3"
        ></textarea></label
      >
      <small class="muted">{reason.length} / {MAX_REASON}</small>
      <div class="buttons">
        <button
          type="button"
          class="primary-button"
          disabled={saving}
          onclick={() => void confirm()}>{saving ? 'Accepting…' : 'Confirm acceptance'}</button
        >
        <button type="button" onclick={() => (confirming = false)}>Cancel</button>
      </div>
    </div>
  {/if}
  {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
  {#if invalid.length && !confirming}
    <ul class="problem">
      {#each invalid as text}<li>{text}</li>{/each}
    </ul>
  {/if}
  {#if error}<ErrorMessage {error} />{/if}
</section>

<style>
  .classification-draft {
    border: 1px solid var(--border, #ccc);
    border-radius: 0.5rem;
    padding: 0.8rem;
    margin: 0.8rem 0;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: baseline;
  }
  .framing {
    border-left: 3px solid var(--warn-text, #a60);
    padding-left: 0.6rem;
  }
  .table-scroll {
    overflow-x: auto;
  }
  .compare {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9rem;
  }
  .compare th,
  .compare td {
    text-align: left;
    vertical-align: top;
    padding: 0.4rem;
    border-bottom: 1px solid var(--border, #ccc);
  }
  .compare p {
    margin: 0.3rem 0;
  }
  .rule {
    font-size: 0.85rem;
  }
  .editor {
    display: grid;
    gap: 0.6rem;
  }
  .editor label,
  .confirm label {
    display: grid;
    gap: 0.2rem;
  }
  .add {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    align-items: end;
  }
  .confirm {
    border: 1px solid var(--border, #ccc);
    border-radius: 0.5rem;
    padding: 0.8rem;
    margin-top: 0.6rem;
  }
  .buttons {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }
</style>
