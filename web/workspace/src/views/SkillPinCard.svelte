<!-- SPDX-License-Identifier: MIT -->
<!-- One pinned source on the skills page (SK-24): the import it pins, and that import's
     report: the skills, what the import's model pass derived (marked as derived), the
     built-ins the pack leaves out, hidden characters, broken links, warnings and the
     files left out. Pack text is untrusted and shown as text, with hidden characters as
     their code points. -->
<script lang="ts">
  import type { SkillPinView } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { reportView } from '../lib/skills';
  import { visible } from '../lib/text';

  let { pin, projectId }: { pin: SkillPinView; projectId: string } = $props();
  const report = $derived(reportView(pin.pinned.report, projectId));
  const where = $derived(
    [pin.pinned.ref, pin.pinned.commit?.slice(0, 12)].filter((part) => part).join(' at '),
  );
</script>

<section class="pin">
  <h3><code>{visible(pin.source)}</code> at revision {pin.revision}</h3>
  <dl class="facts">
    <dt>Imported</dt>
    <dd>
      {formatTime(pin.pinned.created)} from {pin.pinned.kind}
      <code>{visible(pin.pinned.origin)}</code>{#if where}, <code>{visible(where)}</code>{/if}
    </dd>
    <dt>Skills</dt>
    <dd>{pin.skills.map(visible).join(', ') || 'none'}</dd>
    {#if pin.left_out.length || report.leftOut.length}
      <dt>Built-ins left out</dt>
      <dd>
        {[...new Set([...pin.left_out.map(visible), ...report.leftOut])].join(', ')}
        <span class="muted">(a pack's skill of the same name replaces them)</span>
      </dd>
    {/if}
    {#if report.hidden.length}
      <dt>Hidden characters</dt>
      <dd class="problem">
        {#each report.hidden as item, index (index)}
          <code>{item.path}</code>: {item.count}{index < report.hidden.length - 1 ? '; ' : ''}
        {/each}
      </dd>
    {/if}
    {#if report.broken.length}
      <dt>Broken links</dt>
      <dd class="problem">
        {#each report.broken as item, index (index)}
          <code>{item.path}</code> links to <code>{item.link}</code>{index <
          report.broken.length - 1
            ? '; '
            : ''}
        {/each}
      </dd>
    {/if}
    {#if report.skipped.length}
      <dt>Files left out</dt>
      <dd>
        {#each report.skipped as item, index (index)}
          <code>{item.path}</code> ({item.reason}){index < report.skipped.length - 1 ? '; ' : ''}
        {/each}
      </dd>
    {/if}
    {#if report.warnings.length}
      <dt>Warnings</dt>
      <dd>
        <ul>
          {#each report.warnings as warning, index (index)}<li>{warning}</li>{/each}
        </ul>
      </dd>
    {/if}
  </dl>
  {#if report.derived}
    <details class="derived">
      <summary>Derived by {report.derived.by}</summary>
      <p class="muted">
        The import's model pass wrote these from the pack's text. They are not the pack's own.
      </p>
      {#if report.derived.topics.length}
        <h4>Topics</h4>
        <ul>
          {#each report.derived.topics as [topic, skills] (topic)}
            <li><strong>{topic}</strong>: {skills.join(', ')} <span class="mark">derived</span></li>
          {/each}
        </ul>
      {/if}
      {#if report.derived.entry.length}
        <h4>Entry skills</h4>
        <p>{report.derived.entry.join(', ')} <span class="mark">derived</span></p>
      {/if}
      {#if report.derived.descriptions.length}
        <h4>Short descriptions</h4>
        <ul>
          {#each report.derived.descriptions as [skill, text] (skill)}
            <li><code>{skill}</code>: {text} <span class="mark">derived</span></li>
          {/each}
        </ul>
      {/if}
      {#if report.derived.bindings.length}
        <h4>Bindings</h4>
        <ul>
          {#each report.derived.bindings as [name, path] (name)}
            <li><code>{name}</code> = <code>{path}</code> <span class="mark">derived</span></li>
          {/each}
        </ul>
      {/if}
      {#if report.derived.held.length}
        <h4>Held bindings</h4>
        <ul>
          {#each report.derived.held as item (item.variable)}
            <li><code>{item.variable}</code> = <code>{item.path}</code>: {item.reason}</li>
          {/each}
        </ul>
      {/if}
      {#if report.derived.dropped}
        <p class="muted">{report.derived.dropped} derived items were dropped as unfit.</p>
      {/if}
    </details>
  {/if}
</section>

<style>
  .pin {
    border-top: 1px solid var(--border, #ccc);
    padding-top: 0.5rem;
  }
  .mark {
    font-size: 0.8em;
    border: 1px solid currentColor;
    border-radius: 0.25rem;
    padding: 0 0.25rem;
    opacity: 0.75;
  }
</style>
