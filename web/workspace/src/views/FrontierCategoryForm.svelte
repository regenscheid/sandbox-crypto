<!-- SPDX-License-Identifier: MIT -->
<!-- The user's definition of a frontier category, or its revision at the revision read
     (docs/specs/ledger.md, LG-18): its name, target, kind of result, measures with their
     units and directions, condition keys and the reason. Before it is saved, the form
     shows exactly what will be saved (ApprovalText: the text may be an agent's, adopted),
     and for a revision what it changes; the save names the revision read. A conflict saves
     nothing: the form reads the category as it is now and keeps the draft, and the user
     reviews it against that revision before saving again. -->
<script module lang="ts">
  /** Each form's own radio names, so two forms on a page keep their choices apart. */
  let forms = 0;
</script>

<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { Category } from '../lib/api-types';
  import {
    changesText,
    categoryText,
    conflictText,
    definitionBody,
    draftOf,
    emptyDraft,
    formProblem,
    MAX_CONDITIONS,
    MAX_MEASURES,
    MAX_NOTE,
    MAX_UNIT,
    summaryText,
    type CategoryBody,
  } from '../lib/frontierForm';
  import { guardedWrite } from '../lib/mutation';
  import { idle, submit } from '../lib/submit';
  import ApprovalText from './ApprovalText.svelte';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    current = null,
    done,
    oncancel,
  }: {
    projectId: string;
    /** The category as read, to revise it; null to define a new one. */
    current?: Category | null;
    /** After the save: the category as saved. */
    done: (saved: Category) => Promise<void> | void;
    oncancel: () => void;
  } = $props();
  const form = `category-form-${++forms}`;
  // The form is made for one category, or none, and closed after it.
  /** The revision the save names as read; after a conflict, the one read then. */
  let base = $state<Category | null>(untrack(() => current));
  let draft = $state(untrack(() => (current === null ? emptyDraft() : draftOf(current))));
  /** What the user confirms: the body as it will be sent, fixed when the review opens. */
  let reviewing = $state<CategoryBody | null>(null);
  let problem = $state<string | null>(null);
  /** The category as it is now, read after a conflict. */
  let latest = $state<Category | null>(null);
  let conflict = $state<string | null>(null);
  const saving = $state(idle());
  const changes = $derived(
    reviewing !== null && base !== null ? changesText(base, reviewing) : null,
  );

  function review(): void {
    const body = definitionBody($state.snapshot(draft), base);
    problem = formProblem(body);
    if (problem === null) reviewing = body;
  }

  async function save(): Promise<void> {
    const body = reviewing;
    if (body === null) return;
    conflict = null;
    let saved: Category | null = null;
    const ok = await submit(saving, async () => {
      saved = await guardedWrite(
        'frontier-category',
        `${projectId}:${body.category}`,
        body,
        (sent) => api.defineCategory(projectId, sent),
      );
    });
    if (ok && saved !== null) {
      await done(saved);
      return;
    }
    conflict = conflictText(saving.error, body);
    if (conflict === null) return;
    try {
      latest = (await api.frontierDefinition(projectId, body.category)).category;
    } catch {
      latest = null;
    }
  }

  /** Review the draft again as a revision of the category as it is now. */
  function againstLatest(): void {
    if (latest === null) return;
    base = latest;
    draft.category = latest.category;
    latest = null;
    conflict = null;
    saving.error = null;
    reviewing = definitionBody($state.snapshot(draft), base);
  }
</script>

<div class="frontier-action">
  {#if reviewing === null}
    <h3>
      {base === null ? 'Define a frontier category' : `Revise ${base.category}, r${base.revision}`}
    </h3>
    {#if base === null}
      <label
        >Name, such as key-recovery
        <input bind:value={draft.category} maxlength="128" autocomplete="off" /></label
      >
    {/if}
    <label
      >Target: what the results are about, such as a scheme and parameter set
      <textarea bind:value={draft.target} maxlength={MAX_NOTE} rows="2"></textarea></label
    >
    <label
      >Kind of result, such as key recovery cost or a bound
      <textarea bind:value={draft.kind} maxlength={MAX_NOTE} rows="2"></textarea></label
    >
    <fieldset>
      <legend>Measures, each with its unit and whether lower or higher is better</legend>
      <p class="muted">
        Names here and condition keys below: a letter or digit first, then letters, digits, dots,
        dashes or underscores, without spaces, such as <code>time_bits</code> or
        <code>attack_model</code>.
      </p>
      {#each draft.measures as measure, index (index)}
        <p class="measure">
          <label>Name <input bind:value={measure.name} maxlength="128" /></label>
          <label>Unit <input bind:value={measure.unit} maxlength={MAX_UNIT} /></label>
          <span role="radiogroup" aria-label="Which is better">
            {#each ['lower', 'higher'] as const as direction (direction)}
              <label class="direction"
                ><input
                  type="radio"
                  name={`${form}-direction-${index}`}
                  value={direction}
                  bind:group={measure.direction}
                />
                {direction} is better</label
              >
            {/each}
          </span>
          <button
            type="button"
            disabled={draft.measures.length === 1}
            onclick={() => draft.measures.splice(index, 1)}>Remove</button
          >
        </p>
      {/each}
      <button
        type="button"
        disabled={draft.measures.length >= MAX_MEASURES}
        onclick={() => draft.measures.push({ name: '', unit: '', direction: 'lower' })}
        >Add a measure</button
      >
    </fieldset>
    <fieldset>
      <legend>Condition keys, such as attack_model or cost_model</legend>
      {#each draft.conditions as _, index (index)}
        <p class="condition">
          <label>Key <input bind:value={draft.conditions[index]} maxlength="128" /></label>
          <button type="button" onclick={() => draft.conditions.splice(index, 1)}>Remove</button>
        </p>
      {:else}
        <p class="muted">None: every result is compared with every other.</p>
      {/each}
      <button
        type="button"
        disabled={draft.conditions.length >= MAX_CONDITIONS}
        onclick={() => draft.conditions.push('')}>Add a condition key</button
      >
    </fieldset>
    <label
      >Your reason
      <textarea bind:value={draft.reason} maxlength={MAX_NOTE} rows="2"></textarea></label
    >
    <p>
      <button type="button" onclick={review}>Review…</button>
      <button type="button" onclick={oncancel}>Cancel</button>
    </p>
    {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
  {:else}
    <h3>{reviewing.expected === undefined ? 'Define' : 'Revise'} {reviewing.category}</h3>
    <ApprovalText text={summaryText(reviewing)} label="What will be saved, exactly" />
    {#if changes !== null}
      <p class="label">What it changes from r{reviewing.expected}:</p>
      {#if changes.length}
        <ul>
          {#each changes as change, index (index)}<li>{change}</li>{/each}
        </ul>
      {:else}
        <p class="muted">Nothing it compares: only its text or reason.</p>
      {/if}
    {/if}
    {#if conflict}
      <p class="problem" role="alert">{conflict}</p>
      {#if latest}
        <ApprovalText text={categoryText(latest)} label="The category as it is now" />
        <p>
          <button type="button" onclick={againstLatest}
            >Review my draft as a revision of r{latest.revision}</button
          >
        </p>
      {/if}
    {:else if saving.error}
      <ErrorMessage error={saving.error} />
    {/if}
    <p>
      <button type="button" disabled={saving.busy || conflict !== null} onclick={save}
        >{reviewing.expected === undefined
          ? `Define ${reviewing.category}`
          : `Save r${reviewing.expected + 1}`}</button
      >
      <button
        type="button"
        disabled={saving.busy}
        onclick={() => {
          reviewing = null;
          conflict = null;
          latest = null;
        }}>Back to the draft</button
      >
      <button type="button" disabled={saving.busy} onclick={oncancel}>Cancel</button>
    </p>
  {/if}
</div>

<style>
  .measure label,
  .condition label,
  .direction {
    display: inline-block;
    margin-right: 0.6em;
  }
  textarea {
    width: 100%;
    box-sizing: border-box;
  }
  label {
    display: block;
    margin: 0.4em 0;
  }
</style>
