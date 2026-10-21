<!-- SPDX-License-Identifier: MIT -->
<!-- A project's skills (docs/specs/skills.md, SK-24): the skills a run is given now, with
     the short descriptions and topics marked by who set them; the skill settings, which
     the user changes here at the revision shown; the user's overrides of what imports'
     passes derive; and each pinned source with its import's report. After a change, and
     after a conflict, the page reads the skills again; after a conflict it keeps the lists
     typed. Pack text is untrusted and shown as text, hidden characters as code points. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { browserState } from '../lib/browserState';
  import { api } from '../lib/api';
  import type { ProjectSkills } from '../lib/api-types';
  import { projectHref } from '../lib/router';
  import { idLines, overridesView } from '../lib/skills';
  import { formatTime } from '../lib/format';
  import { idle, submit } from '../lib/submit';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';
  import PaneResizer from './PaneResizer.svelte';
  import SkillPinCard from './SkillPinCard.svelte';

  let { projectId }: { projectId: string } = $props();
  let shown = $state<ProjectSkills | null>(null);
  let failure = $state<unknown>(null);
  const saving = $state(idle());
  const scope = untrack(() => `skills:${projectId}`);
  const restored = untrack(() => browserState.load(scope));
  const kept = restored.draft ?? {};
  let stored = $state(restored.stored);
  const restoredDraft = kept.dirty === 'yes';
  let baseRevision = $state(Number(kept.base) || 0);
  let always = $state(kept.always ?? '');
  let excluded = $state(kept.excluded ?? '');
  let view = $state<'catalog' | 'sources' | 'loading'>(
    kept.view === 'sources' || kept.view === 'loading' ? kept.view : 'catalog',
  );
  let query = $state(kept.query ?? '');
  let source = $state(kept.source ?? 'all');
  let topic = $state(kept.topic ?? 'all');
  let loading = $state(kept.loading ?? 'all');
  let selectedSkill = $state<string | null>(kept.selectedSkill || null);
  let selectedSource = $state<string | null>(kept.selectedSource || null);
  let detailWidth = $state(
    Math.min(Number(kept.width) || 460, Math.max(320, Math.floor(window.innerWidth / 2))),
  );
  let viewportWidth = $state(window.innerWidth);
  const maxWidth = $derived(Math.max(320, Math.floor(viewportWidth / 2)));
  const overrides = $derived(shown === null ? null : overridesView(shown.settings.overrides));
  const draftAlways = $derived(idLines(always));
  const draftExcluded = $derived(idLines(excluded));
  const dirty = $derived(
    shown !== null &&
      (draftAlways.join('\n') !== shown.settings.always.join('\n') ||
        draftExcluded.join('\n') !== shown.settings.excluded.join('\n')),
  );
  $effect(() => {
    stored = browserState.save(scope, {
      always,
      excluded,
      base: String(baseRevision),
      dirty: shown === null ? (restoredDraft ? 'yes' : 'no') : dirty ? 'yes' : 'no',
      query,
      source,
      topic,
      loading,
      view,
      selectedSkill: selectedSkill ?? '',
      selectedSource: selectedSource ?? '',
      width: String(detailWidth),
    });
  });
  const sources = $derived(
    shown === null ? [] : [...new Set(shown.effective.map((item) => item.source))].sort(),
  );
  const topics = $derived(
    shown === null
      ? []
      : [
          ...new Set(
            shown.effective
              .map((item) => item.topic)
              .filter((item): item is string => item !== null),
          ),
        ].sort(),
  );
  const catalog = $derived(
    shown === null
      ? []
      : shown.effective.filter((item) => {
          const mode = skillMode(item.id);
          return (
            (source === 'all' || item.source === source) &&
            (topic === 'all' || item.topic === topic) &&
            (loading === 'all' || mode === loading) &&
            `${item.id} ${item.name} ${item.description} ${item.topic ?? ''}`
              .toLocaleLowerCase()
              .includes(query.trim().toLocaleLowerCase())
          );
        }),
  );
  const selected = $derived(shown?.effective.find((item) => item.id === selectedSkill) ?? null);
  const pin = $derived(shown?.pins.find((item) => item.source === selectedSource) ?? null);
  const changed = $derived.by(() => {
    const current = shown;
    if (current === null) return [];
    return [
      ...new Set([
        ...current.settings.always,
        ...current.settings.excluded,
        ...draftAlways,
        ...draftExcluded,
      ]),
    ]
      .filter((id) => {
        const before = current.settings.excluded.includes(id)
          ? 'excluded'
          : current.settings.always.includes(id)
            ? 'always'
            : 'available';
        return before !== skillMode(id);
      })
      .sort();
  });

  const MODES = [
    ['available', 'On request'],
    ['always', 'Always'],
    ['excluded', 'Off'],
  ] as const;

  function skillMode(id: string): 'available' | 'always' | 'excluded' {
    if (draftExcluded.includes(id)) return 'excluded';
    return draftAlways.includes(id) ? 'always' : 'available';
  }

  function setMode(id: string, mode: 'available' | 'always' | 'excluded') {
    always = idLines(always)
      .filter((item) => item !== id)
      .join('\n');
    excluded = idLines(excluded)
      .filter((item) => item !== id)
      .join('\n');
    if (mode === 'always') always = [...idLines(always), id].join('\n');
    if (mode === 'excluded') excluded = [...idLines(excluded), id].join('\n');
  }

  function discard() {
    if (shown === null) return;
    always = shown.settings.always.join('\n');
    excluded = shown.settings.excluded.join('\n');
    baseRevision = shown.settings.revision;
    saving.error = null;
  }

  /** Read the skills again; after a conflict, keep the lists the user typed. */
  async function load(keep = false) {
    try {
      const found = await api.skills(projectId);
      shown = found;
      if (!keep) {
        always = found.settings.always.join('\n');
        excluded = found.settings.excluded.join('\n');
        baseRevision = found.settings.revision;
      }
      if (selectedSkill === null) selectedSkill = found.effective[0]?.id ?? null;
      if (selectedSource === null) selectedSource = found.pins[0]?.source ?? null;
      failure = null;
    } catch (caught) {
      failure = caught;
    }
  }
  void load(restoredDraft);

  async function save() {
    if (shown === null || !dirty) return;
    const expected = baseRevision;
    await submit(
      saving,
      async () => {
        await api.changeSkillSettings(projectId, {
          expected,
          always: idLines(always),
          excluded: idLines(excluded),
        });
        await load();
      },
      () => load(true),
    );
  }
</script>

<svelte:window
  onresize={() => {
    viewportWidth = window.innerWidth;
    detailWidth = Math.min(detailWidth, maxWidth);
  }}
/>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
</nav>
<h1>Skills</h1>
{#if restored.damaged}
  <p role="alert">
    Saved skill browser data could not be restored. Check your loading choices before continuing.
  </p>
{/if}
{#if (dirty || (shown === null && restoredDraft)) && !stored}
  <p role="alert">
    These skill loading edits are kept only in this tab. Copy them before reloading or closing the
    tab.
  </p>
{/if}
<p class="muted">
  Browse the skills future runs can use, inspect pinned sources, and review loading choices before
  saving them.
</p>
{#if failure}
  <ErrorMessage error={failure} />
{:else if shown === null}
  <p class="muted">Loading…</p>
{:else}
  <div class="browser-tabs" role="tablist" aria-label="Skills views">
    <button
      type="button"
      role="tab"
      aria-selected={view === 'catalog'}
      class:active={view === 'catalog'}
      onclick={() => (view = 'catalog')}>Catalog <span>{shown.effective.length}</span></button
    >
    <button
      type="button"
      role="tab"
      aria-selected={view === 'sources'}
      class:active={view === 'sources'}
      onclick={() => (view = 'sources')}>Pinned sources <span>{shown.pins.length}</span></button
    >
    <button
      type="button"
      role="tab"
      aria-selected={view === 'loading'}
      class:active={view === 'loading'}
      onclick={() => (view = 'loading')}
      >Loading rules {#if dirty}<span class="pending">{changed.length} pending</span>{/if}</button
    >
  </div>

  {#if view === 'catalog' || view === 'loading'}
    <div class="filterbar" aria-label="Filter skills">
      <label
        >Search <input type="search" bind:value={query} placeholder="Name, purpose, or ID" /></label
      >
      <label
        >Source <select bind:value={source}
          ><option value="all">All sources</option>{#each sources as name}<option value={name}
              >{visible(name)}</option
            >{/each}</select
        ></label
      >
      <label
        >Topic <select bind:value={topic}
          ><option value="all">All topics</option>{#each topics as name}<option value={name}
              >{visible(name)}</option
            >{/each}</select
        ></label
      >
      <label
        >Loading <select bind:value={loading}
          ><option value="all">All choices</option><option value="available">On request</option
          ><option value="always">Always loaded</option><option value="excluded">Off</option
          ></select
        ></label
      >
    </div>
  {/if}

  {#if view === 'catalog'}
    {#if dirty && shown}<div class="save-bar" role="status">
        <span
          >{changed.length}
          {changed.length === 1 ? 'skill change' : 'skill changes'} not saved yet</span
        ><button
          type="button"
          class="primary-button"
          disabled={saving.busy || baseRevision !== shown.settings.revision}
          onclick={() => void save()}>Save</button
        ><button type="button" disabled={saving.busy} onclick={discard}>Discard</button>
      </div>{/if}
    <p class="muted">
      {catalog.length} matching skills. Choices here affect future runs; each run records what it actually
      loaded.
    </p>
    <div class="browser-columns" style:--detail-width={`${detailWidth}px`}>
      <section class="browser-list" aria-label="Skill catalog">
        {#each catalog as skill (skill.id)}
          <div class="skill-row" class:selected={selectedSkill === skill.id}>
            <button
              type="button"
              class="browser-row"
              class:selected={selectedSkill === skill.id}
              aria-pressed={selectedSkill === skill.id}
              onclick={() => (selectedSkill = skill.id)}
            >
              <span class="row-top"><strong>{visible(skill.name)}</strong></span>
              <span class="row-description">{visible(skill.short || skill.description)}</span>
              <span class="muted"
                >{visible(skill.source)}{#if skill.topic}
                  · {visible(skill.topic)}{/if}</span
              >
            </button>
            <div
              class="segmented skill-mode"
              role="group"
              aria-label={`When runs load ${skill.name}`}
            >
              {#each MODES as [mode, label] (mode)}<button
                  type="button"
                  aria-pressed={skillMode(skill.id) === mode}
                  onclick={() => setMode(skill.id, mode)}>{label}</button
                >{/each}
            </div>
          </div>
        {:else}
          <p class="muted">
            No skills match these filters. Excluded IDs without metadata are listed under Loading
            rules.
          </p>
        {/each}
      </section>
      <PaneResizer bind:width={detailWidth} {maxWidth} label="Resize skill details" />
      <aside class="browser-detail" aria-label="Skill details">
        {#if selected}
          <p class="eyebrow">
            {visible(selected.source)} · {selected.entry ? 'entry skill' : 'skill'}
          </p>
          <h2>{visible(selected.name)}</h2>
          <p>{visible(selected.description)}</p>
          <dl class="facts">
            <dt>ID</dt>
            <dd><code>{visible(selected.id)}</code></dd>
            <dt>Revision</dt>
            <dd><code>{visible(selected.revision)}</code></dd>
            <dt>Topic</dt>
            <dd>
              {selected.topic ? visible(selected.topic) : 'Unspecified'}{#if selected.topic_by}
                · {selected.topic_by === 'user' ? 'set by you' : 'derived'}{/if}
            </dd>
            <dt>Short description</dt>
            <dd>
              {selected.short ? visible(selected.short) : 'Unspecified'}{#if selected.short_by}
                · {selected.short_by === 'user' ? 'set by you' : 'derived'}{/if}
            </dd>
          </dl>
          <fieldset class="mode-choice">
            <legend>For future runs</legend>
            {#each ['available', 'always', 'excluded'] as mode}
              <label
                ><input
                  type="radio"
                  name="skill-mode"
                  checked={skillMode(selected.id) === mode}
                  onchange={() => setMode(selected.id, mode as 'available' | 'always' | 'excluded')}
                />{mode === 'always'
                  ? 'Always loaded'
                  : mode === 'excluded'
                    ? 'Off: never offered'
                    : 'On request'}</label
              >
            {/each}
          </fieldset>
          <p class="muted">
            These choices do not change historical runs. Save pending changes under Loading rules.
          </p>
        {:else}<p class="muted">Select a skill to see its details.</p>{/if}
      </aside>
    </div>
  {:else if view === 'sources'}
    <div class="browser-columns" style:--detail-width={`${detailWidth}px`}>
      <section class="browser-list" aria-label="Pinned sources">
        {#each shown.pins as item (item.source)}
          <button
            type="button"
            class="browser-row"
            class:selected={selectedSource === item.source}
            aria-pressed={selectedSource === item.source}
            onclick={() => (selectedSource = item.source)}
          >
            <span class="row-top"
              ><strong>{visible(item.source)}</strong><span class="mode">r{item.revision}</span
              ></span
            >
            <span class="muted"
              >{item.skills.length} skills · imported {formatTime(item.pinned.created)}</span
            >
          </button>
        {:else}<p class="muted">No sources are pinned.</p>{/each}
      </section>
      <PaneResizer bind:width={detailWidth} {maxWidth} label="Resize source details" />
      <aside class="browser-detail" aria-label="Source details">
        {#if pin}
          <SkillPinCard {pin} {projectId} />
          <h3>Helper bindings</h3>
          {@const bindings = shown.bindings.find((item) => item.source === pin.source)}
          {#if bindings && (Object.keys(bindings.set).length || bindings.held.length)}
            <ul>
              {#each Object.entries(bindings.set) as [name, path]}<li>
                  <code>{visible(name)}</code> = <code>{visible(path)}</code> (set)
                </li>{/each}{#each bindings.held as held}<li>
                  <code>{visible(held.variable)}</code> held: {visible(held.reason)}
                </li>{/each}
            </ul>
          {:else}<p class="muted">No helper bindings recorded.</p>{/if}
        {:else}<p class="muted">Select a source to inspect its import report.</p>{/if}
      </aside>
    </div>
    <details class="overrides">
      <summary>Your overrides</summary>
      <p class="muted">
        These override the import pass. Change them with <code>skills override</code>.
      </p>
      {#if overrides && (overrides.skills.length || overrides.unbound.length || overrides.approved.length || overrides.prefixes.length)}<ul
        >
          {#each overrides.skills as item}<li>
              <code>{item.skill}</code>: {item.fields.join('; ')}
            </li>{/each}{#each overrides.unbound as item}<li>
              <code>{item.source}</code>: removed {item.variables.join(', ')}
            </li>{/each}{#each overrides.approved as item}<li>
              <code>{item.source}</code>: approved {item.variables.join(', ')}
            </li>{/each}{#each overrides.prefixes as item}<li>
              <code>{item.source}</code>: prefix <code>{item.prefix}</code>
            </li>{/each}
        </ul>{:else}<p class="muted">No overrides.</p>{/if}
    </details>
  {:else}
    <section class="loading-rules">
      <h2>Loading rules</h2>
      <p class="muted">
        Revision {shown.settings.revision}. Available skills can be loaded when needed. Always
        loaded skills enter every future run; excluded skills are not offered. Historical runs keep
        their own pinned skill lists.
      </p>
      <p>{catalog.length} skills match the filters above.</p>
      <div class="bulk-actions" aria-label="Apply to matching skills">
        <span>Set matching skills:</span><button
          type="button"
          onclick={() => catalog.forEach((item) => setMode(item.id, 'available'))}>Available</button
        ><button type="button" onclick={() => catalog.forEach((item) => setMode(item.id, 'always'))}
          >Always loaded</button
        ><button
          type="button"
          onclick={() => catalog.forEach((item) => setMode(item.id, 'excluded'))}>Excluded</button
        >
      </div>
      <div class="rules-list">
        {#each catalog as skill (skill.id)}<label
            ><span
              ><strong>{visible(skill.name)}</strong><small class="muted">{visible(skill.id)}</small
              ></span
            ><select
              value={skillMode(skill.id)}
              onchange={(event) =>
                setMode(skill.id, event.currentTarget.value as 'available' | 'always' | 'excluded')}
              ><option value="available">Available</option><option value="always"
                >Always loaded</option
              ><option value="excluded">Excluded</option></select
            ></label
          >{:else}<p class="muted">No skills match the current filters.</p>{/each}
      </div>
      <details>
        <summary>Edit IDs directly, including excluded skills without metadata</summary>
        <div class="manual-rules">
          <label
            >Always loaded IDs, one per line<textarea rows="5" bind:value={always}
            ></textarea></label
          ><label
            >Excluded IDs, one per line<textarea rows="5" bind:value={excluded}></textarea></label
          >
        </div>
      </details>
      {#if saving.error}<ErrorMessage error={saving.error} />
        <p class="muted">
          The latest revision was loaded; your choices are retained below for comparison.
        </p>{/if}
      {#if dirty}<section class="pending-review" aria-label="Review pending skill changes">
          <h3>Review pending changes</h3>
          {#if changed.length}<ul>
              {#each changed as id}<li>
                  <code>{visible(id)}</code> → {skillMode(id) === 'always'
                    ? 'Always loaded'
                    : skillMode(id) === 'excluded'
                      ? 'Excluded'
                      : 'Available'}
                </li>{/each}
            </ul>{:else}<p class="muted">
              The ID lists changed order; saving will normalize them.
            </p>{/if}
          {#if baseRevision !== shown.settings.revision}<p role="status">
              This draft began at revision {baseRevision}; current revision is {shown.settings
                .revision}. Compare the pending choices with the current rules before applying them.
            </p>
            <button onclick={() => (baseRevision = shown!.settings.revision)}
              >Use current revision for this reviewed draft</button
            >{/if}
          <div class="bulk-actions">
            <button
              type="button"
              disabled={saving.busy || baseRevision !== shown.settings.revision}
              onclick={() => void save()}>Save at revision {shown.settings.revision}</button
            ><button type="button" disabled={saving.busy} onclick={discard}>Discard draft</button>
          </div>
        </section>{:else}<p class="muted">No pending changes.</p>{/if}
    </section>
  {/if}
{/if}

<style>
  .browser-tabs,
  .filterbar,
  .bulk-actions {
    display: flex;
    gap: 0.6rem;
    flex-wrap: wrap;
    align-items: end;
  }
  .browser-tabs {
    border-bottom: 1px solid var(--border, #ccc);
    margin: 1.25rem 0;
  }
  .browser-tabs button {
    border: 0;
    border-bottom: 2px solid transparent;
    background: transparent;
    padding: 0.65rem 0.9rem;
    cursor: pointer;
  }
  .browser-tabs button.active {
    border-bottom-color: currentColor;
    font-weight: 700;
  }
  .browser-tabs span,
  .pending {
    margin-left: 0.35rem;
    opacity: 0.7;
  }
  .filterbar {
    padding: 0.75rem;
    border: 1px solid var(--border, #ccc);
    border-radius: 0.5rem;
    margin-bottom: 0.75rem;
  }
  .filterbar label {
    display: grid;
    gap: 0.2rem;
    font-size: 0.85rem;
  }
  .filterbar input {
    min-width: 15rem;
  }
  .filterbar input,
  .filterbar select {
    padding: 0.4rem;
  }
  .browser-columns {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 8px minmax(320px, var(--detail-width));
    min-height: 29rem;
    border: 1px solid var(--border, #ccc);
    border-radius: 0.5rem;
    overflow: hidden;
  }
  .browser-list {
    overflow: auto;
    max-height: 70vh;
  }
  .browser-row {
    display: grid;
    gap: 0.3rem;
    width: 100%;
    padding: 0.85rem;
    text-align: left;
    border: 0;
    border-bottom: 1px solid var(--border, #ccc);
    background: transparent;
    cursor: pointer;
  }
  .skill-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding-right: 8px;
    border-bottom: 1px solid var(--border, #ccc);
  }
  .skill-row .browser-row {
    flex: 1;
    min-width: 0;
    border-bottom: 0;
  }
  .skill-row.selected,
  .skill-row:hover {
    background: var(--surface-raised, #f1f4f3);
  }
  .skill-row .browser-row:hover,
  .skill-row .browser-row.selected {
    background: transparent;
  }
  .skill-mode {
    flex: none;
  }
  .skill-mode button {
    font-size: 12px;
    padding: 0 8px;
  }
  .save-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    margin: 8px 0;
    border: 1px solid var(--accent-line, #ccc);
    border-radius: 10px;
    background: var(--accent-soft, #eef);
  }
  .save-bar span {
    flex: 1;
  }
  .browser-row:hover,
  .browser-row.selected {
    background: var(--surface-raised, #f1f4f3);
  }
  .row-top {
    display: flex;
    justify-content: space-between;
    gap: 0.6rem;
  }
  .row-description {
    display: -webkit-box;
    line-clamp: 2;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .mode {
    font-size: 0.8rem;
    white-space: nowrap;
  }
  .browser-detail {
    overflow: auto;
    max-height: 70vh;
    padding: 1rem;
    border-left: 1px solid var(--border, #ccc);
  }
  .mode-choice {
    display: grid;
    gap: 0.4rem;
    margin-top: 1rem;
  }
  .mode-choice label {
    display: flex;
    gap: 0.4rem;
    align-items: center;
  }
  .overrides,
  .loading-rules {
    margin-top: 1rem;
  }
  .rules-list {
    margin: 1rem 0;
    border-top: 1px solid var(--border, #ccc);
  }
  .rules-list label {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    align-items: center;
    border-bottom: 1px solid var(--border, #ccc);
    padding: 0.55rem;
  }
  .rules-list small {
    display: block;
  }
  .manual-rules {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem;
    margin: 0.75rem 0;
  }
  .manual-rules label {
    display: grid;
    gap: 0.3rem;
  }
  .manual-rules textarea {
    width: 100%;
    box-sizing: border-box;
    font-family: monospace;
  }
  .pending-review {
    border: 1px solid var(--border, #ccc);
    padding: 1rem;
    margin-top: 1rem;
    border-radius: 0.5rem;
  }
  @media (max-width: 800px) {
    .browser-columns {
      display: block;
    }
    .browser-columns :global(.pane-resizer) {
      display: none;
    }
    .browser-list,
    .browser-detail {
      max-height: none;
    }
    .browser-detail {
      border-left: 0;
      border-top: 1px solid var(--border, #ccc);
    }
    .manual-rules {
      grid-template-columns: 1fr;
    }
  }
</style>
