<!-- SPDX-License-Identifier: MIT -->
<!-- A project's skills (docs/specs/skills.md, SK-24): the skills a run is given now, with
     the short descriptions and topics marked by who set them; the skill settings, which
     the user changes here at the revision shown; the user's overrides of what imports'
     passes derive; and each pinned source with its import's report. After a change, and
     after a conflict, the page reads the skills again; after a conflict it keeps the lists
     typed. Pack text is untrusted and shown as text, hidden characters as code points. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { ProjectSkills } from '../lib/api-types';
  import { projectHref } from '../lib/router';
  import { idLines, overridesView } from '../lib/skills';
  import { idle, submit } from '../lib/submit';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';
  import SkillPinCard from './SkillPinCard.svelte';
  import SkillTable from './SkillTable.svelte';

  let { projectId }: { projectId: string } = $props();
  let shown = $state<ProjectSkills | null>(null);
  let failure = $state<unknown>(null);
  const saving = $state(idle());
  let always = $state('');
  let excluded = $state('');
  const overrides = $derived(shown === null ? null : overridesView(shown.settings.overrides));

  /** Read the skills again; after a conflict, keep the lists the user typed. */
  async function load(keep = false) {
    try {
      const found = await api.skills(projectId);
      shown = found;
      if (!keep) {
        always = found.settings.always.join('\n');
        excluded = found.settings.excluded.join('\n');
      }
      failure = null;
    } catch (caught) {
      failure = caught;
    }
  }
  void load();

  async function save() {
    if (shown === null) return;
    const expected = shown.settings.revision;
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

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
</nav>
<h1>Skills</h1>
<p>
  The skills this project's runs are given: the built-in skills and the packs it pins, less those it
  excludes. A run sees each skill's name and description, and loads the ones it needs. Pack text is
  shown as written, with hidden characters as their code points.
</p>
{#if failure}
  <ErrorMessage error={failure} />
{:else if shown === null}
  <p class="muted">Loading…</p>
{:else}
  <h2>The skills a run is given ({shown.effective.length})</h2>
  {#if shown.effective.length === 0}
    <p class="muted">None: the project pins no source.</p>
  {:else}
    <SkillTable skills={shown.effective} always={shown.settings.always} />
  {/if}

  <h2>Settings (revision {shown.settings.revision})</h2>
  {#if saving.error}<ErrorMessage error={saving.error} />{/if}
  <form
    class="settings"
    onsubmit={(event) => {
      event.preventDefault();
      void save();
    }}
  >
    <label>
      Loaded in full at every run's start, one skill ID a line
      <textarea rows="4" bind:value={always} placeholder="builtin/name"></textarea>
    </label>
    <label>
      Excluded from the project's runs, one skill ID a line
      <textarea rows="4" bind:value={excluded} placeholder="source/name"></textarea>
    </label>
    <button type="submit" disabled={saving.busy}>Save at revision {shown.settings.revision}</button>
  </form>

  <h2>Your overrides</h2>
  {#if overrides && (overrides.skills.length || overrides.unbound.length || overrides.approved.length || overrides.prefixes.length)}
    <p class="muted">
      What wins over the imports' passes. Change them with <code>skills override</code>.
    </p>
    <ul>
      {#each overrides.skills as item (item.skill)}
        <li><code>{item.skill}</code>: {item.fields.join('; ')}</li>
      {/each}
      {#each overrides.unbound as item (item.source)}
        <li>
          <code>{item.source}</code>'s derived bindings taken away: {item.variables.join(', ')}
        </li>
      {/each}
      {#each overrides.approved as item (item.source)}
        <li>
          <code>{item.source}</code>'s derived bindings approved: {item.variables.join(', ')}
        </li>
      {/each}
      {#each overrides.prefixes as item (item.source)}
        <li><code>{item.source}</code>'s variables also start with <code>{item.prefix}</code></li>
      {/each}
    </ul>
  {:else}
    <p class="muted">None. Set them with <code>skills override</code>.</p>
  {/if}

  <h2>Bindings</h2>
  {#if shown.bindings.length}
    <p class="muted">
      The environment variables the import's pass found for the packs' helpers. Runs set those
      namespaced to the pack, or approved with <code>skills override --approve</code>; the rest are
      held.
    </p>
    <ul>
      {#each shown.bindings as item (item.source)}
        {#each Object.entries(item.set) as [name, path] (name)}
          <li><code>{visible(name)}</code> = <code>{visible(path)}</code> (set)</li>
        {/each}
        {#each item.held as held (held.variable)}
          <li>
            <code>{visible(held.variable)}</code> = <code>{visible(held.path)}</code>, held:
            {visible(held.reason)}
          </li>
        {/each}
      {/each}
    </ul>
  {:else}
    <p class="muted">None: no pass found bindings for the pinned packs.</p>
  {/if}

  <h2>Pinned sources ({shown.pins.length})</h2>
  {#each shown.pins as pin (pin.source)}
    <SkillPinCard {pin} {projectId} />
  {/each}
{/if}

<style>
  .settings label {
    display: block;
    margin-bottom: 0.5rem;
  }
  .settings textarea {
    display: block;
    width: 100%;
    font-family: monospace;
  }
</style>
