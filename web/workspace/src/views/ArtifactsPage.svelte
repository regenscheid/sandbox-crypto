<!-- SPDX-License-Identifier: MIT -->
<!-- Every file the project's runs recorded (research artifacts, RA-37): the results, or all
     files, newest first a page at a time, in a table whose columns sort and resize, with
     the chosen file's details beside it and "Open" showing it in place (ArtifactViewer). -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { ArtifactEntry } from '../lib/api-types';
  import { type ArtifactGroup, NO_FILTER, whoText } from '../lib/artifactFiles';
  import {
    clampWidth,
    COLUMNS,
    firstDirection,
    kindText,
    shortTime,
    sortEntries,
    splitPath,
    type ColumnKey,
    type SortDirection,
  } from '../lib/artifactGrid';
  import { browserState } from '../lib/browserState';
  import { formatTime } from '../lib/format';
  import { emptyList, PagedList } from '../lib/pagedList';
  import { sizeText } from '../lib/pdfAnchors';
  import ArtifactViewer from './ArtifactViewer.svelte';
  import ErrorMessage from './ErrorMessage.svelte';

  let { projectId }: { projectId: string } = $props();
  const scope = untrack(() => `artifacts-page:${projectId}`);
  const kept = untrack(() => browserState.load(scope).draft ?? {});
  const keptWidths = (() => {
    try {
      return JSON.parse(kept.widths ?? '{}') as Partial<Record<ColumnKey, number>>;
    } catch {
      return {};
    }
  })();
  let group = $state<ArtifactGroup>(kept.group === 'files' ? 'files' : 'results');
  let sortKey = $state<ColumnKey>(
    COLUMNS.find((column) => column.key === kept.sortKey)?.key ?? 'when',
  );
  let direction = $state<SortDirection>(kept.direction === 'asc' ? 'asc' : 'desc');
  let widths = $state<Record<ColumnKey, number>>(
    Object.fromEntries(
      COLUMNS.map((column) => [column.key, clampWidth(keptWidths[column.key] ?? column.width)]),
    ) as Record<ColumnKey, number>,
  );
  let contains = $state('');
  let filter = $state({ ...NO_FILTER });
  let selected = $state<string | null>(null);
  let opened = $state(false);
  $effect(() => {
    browserState.save(scope, {
      group,
      sortKey,
      direction,
      widths: JSON.stringify(widths),
    });
  });

  let viewer = $state(false);
  const artifacts = $state(emptyList<ArtifactEntry, number>());
  const list = new PagedList(artifacts, async (before) => {
    const page = await api.artifacts(projectId, before, group, filter);
    return {
      items: page.artifacts,
      next: page.next_before,
      shown: () => {
        viewer = page.viewer;
      },
    };
  });
  $effect(() => {
    void [group, filter.contains];
    selected = null;
    opened = false;
    void list.restart();
  });
  const rows = $derived(sortEntries(artifacts.items, sortKey, direction));
  const chosen = $derived(rows.find((entry) => entry.artifact_id === selected) ?? null);
  const template = $derived(COLUMNS.map((column) => `${widths[column.key]}px`).join(' '));

  function sortBy(key: ColumnKey) {
    if (sortKey === key) direction = direction === 'asc' ? 'desc' : 'asc';
    else {
      sortKey = key;
      direction = firstDirection(key);
    }
  }
  function resize(key: ColumnKey, event: PointerEvent) {
    event.preventDefault();
    const handle = event.currentTarget as HTMLElement;
    handle.setPointerCapture(event.pointerId);
    const start = event.clientX,
      from = widths[key];
    const move = (moved: PointerEvent) => {
      widths[key] = clampWidth(from + moved.clientX - start);
    };
    const stop = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', stop);
      handle.removeEventListener('pointercancel', stop);
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', stop);
    handle.addEventListener('pointercancel', stop);
  }
  function resizeByKey(key: ColumnKey, event: KeyboardEvent) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    widths[key] = clampWidth(widths[key] + (event.key === 'ArrowRight' ? 16 : -16));
  }
  const cell = (entry: ArtifactEntry, key: ColumnKey): string => {
    switch (key) {
      case 'kind':
        return kindText(entry);
      case 'revision':
        return String(entry.revision);
      case 'size':
        return entry.deleted ? '—' : sizeText(entry.size);
      case 'when':
        return shortTime(entry.created_at);
      default:
        return '';
    }
  };
</script>

<div class="page-head">
  <div>
    <h1>Artifacts</h1>
    <p class="muted">Every file the agents' calls wrote, with its revisions</p>
  </div>
  <div class="segmented" role="tablist" aria-label="Files shown">
    <button
      type="button"
      role="tab"
      aria-selected={group === 'results'}
      onclick={() => (group = 'results')}>Results</button
    ><button
      type="button"
      role="tab"
      aria-selected={group === 'files'}
      onclick={() => (group = 'files')}>All files</button
    >
  </div>
  <label class="sr-only" for="artifact-search">Path contains</label>
  <input
    id="artifact-search"
    type="search"
    placeholder="Path contains…"
    bind:value={contains}
    onchange={() => (filter = { ...NO_FILTER, contains: contains.trim() })}
  />
</div>

{#if opened && chosen}
  <ArtifactViewer {projectId} entry={chosen} {viewer} close={() => (opened = false)} />
{:else}
  <div class="split">
    <section class="grid-wrap" aria-label="Files">
      <div role="table" aria-label="Artifacts" class="grid" style:--columns={template}>
        <div role="row" class="grid-row grid-head">
          {#each COLUMNS as column (column.key)}
            <div
              role="columnheader"
              aria-sort={sortKey === column.key
                ? direction === 'asc'
                  ? 'ascending'
                  : 'descending'
                : 'none'}
              class="head-cell"
            >
              <button
                type="button"
                class="sort-button"
                class:sorted={sortKey === column.key}
                onclick={() => sortBy(column.key)}
                ><span>{column.label}</span><span aria-hidden="true" class="arrow"
                  >{sortKey === column.key ? (direction === 'asc' ? '↑' : '↓') : '↕'}</span
                ></button
              >
              <button
                type="button"
                class="resize-handle"
                aria-label={`Resize the ${column.label} column`}
                title="Drag, or use the arrow keys"
                onpointerdown={(event) => resize(column.key, event)}
                onkeydown={(event) => resizeByKey(column.key, event)}
                ><span aria-hidden="true"></span></button
              >
            </div>
          {/each}
        </div>
        {#each rows as entry (entry.artifact_id)}
          {@const path = splitPath(entry.path)}
          <div
            role="row"
            class="grid-row"
            class:chosen={selected === entry.artifact_id}
            class:deleted={entry.deleted}
            tabindex="0"
            onclick={() => (selected = entry.artifact_id)}
            ondblclick={() => {
              selected = entry.artifact_id;
              opened = true;
            }}
            onkeydown={(event) => {
              if (event.key === 'Enter' && event.target === event.currentTarget) {
                selected = entry.artifact_id;
                opened = true;
              }
            }}
          >
            {#each COLUMNS as column (column.key)}
              <div role="cell" class="cell">
                {#if column.key === 'name'}<span class="file-name"><bdi>{path.name}</bdi></span
                  ><span class="file-folder"><bdi>{path.folder || '/'}</bdi></span>{:else}{cell(
                    entry,
                    column.key,
                  )}{/if}
              </div>
            {/each}
          </div>
        {/each}
      </div>
      {#if artifacts.items.length === 0 && !artifacts.loading && !artifacts.error}
        <p class="empty-note">
          {group === 'results'
            ? 'No results yet. An agent publishes a file with publish; a delegated run declares its results.'
            : 'No recorded files. Every file a call writes is recorded, unless the project records results only or an ignore pattern leaves it out.'}
        </p>
      {/if}
      {#if artifacts.loading}<p class="muted">Loading…</p>{/if}
      {#if artifacts.error}<ErrorMessage error={artifacts.error} />{/if}
      <div class="grid-foot">
        {#if artifacts.next !== null}<button
            type="button"
            disabled={artifacts.loading}
            onclick={() => list.more()}>Show more</button
          >{/if}
        {#if artifacts.items.length}<span class="muted"
            >{artifacts.items.length} files loaded{artifacts.next !== null
              ? '; sorting orders the files loaded'
              : ''}</span
          >{/if}
      </div>
    </section>
    <aside class="details" aria-label="Selected file">
      {#if chosen}
        {@const path = splitPath(chosen.path)}
        <div>
          <strong class="details-name"><bdi>{path.name}</bdi></strong>
          <small class="muted"
            ><bdi>{path.folder || '/'}</bdi> · {kindText(chosen)} · {chosen.deleted
              ? 'deleted'
              : sizeText(chosen.size)}</small
          >
        </div>
        <div class="details-actions">
          <button type="button" class="primary-button" onclick={() => (opened = true)}>Open</button>
          {#if chosen.content_retained && !chosen.deleted}<a
              class="button-link"
              download
              href={`/api/v0/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(chosen.artifact_id)}/revisions/${chosen.revision}/content?sha256=${encodeURIComponent(chosen.sha256)}`}
              >Download</a
            >{/if}
        </div>
        <dl class="facts">
          <dt>Revision</dt>
          <dd>{chosen.revision}</dd>
          <dt>Written</dt>
          <dd>{formatTime(chosen.created_at)}</dd>
          <dt>By</dt>
          <dd><bdi>{whoText(chosen)}</bdi></dd>
          <dt>Result</dt>
          <dd>{chosen.result ? 'Yes' : 'No'}</dd>
        </dl>
      {:else}
        <p class="empty-note">Choose a file to see its details. Double-click opens it.</p>
      {/if}
    </aside>
  </div>
{/if}

<style>
  .split {
    display: flex;
    flex-wrap: wrap;
    border-top: 1px solid var(--border);
    margin: 0 -32px;
  }
  .grid-wrap {
    flex: 1 1 480px;
    min-width: 0;
    padding: 8px 24px;
    overflow-x: auto;
  }
  .grid {
    display: flex;
    flex-direction: column;
    width: max-content;
    min-width: 100%;
  }
  .grid-row {
    display: grid;
    grid-template-columns: var(--columns);
    border-top: 1px solid var(--subtle);
    cursor: default;
  }
  .grid-row:not(.grid-head):hover {
    background: var(--panel);
  }
  .grid-row.chosen {
    background: var(--subtle);
  }
  .grid-row.deleted {
    color: var(--muted);
    text-decoration: line-through;
  }
  .grid-head {
    border-top: 0;
    border-bottom: 1px solid var(--border);
  }
  .head-cell {
    position: relative;
    display: flex;
    min-width: 0;
  }
  .sort-button {
    display: flex;
    align-items: center;
    gap: 6px;
    width: 100%;
    min-width: 0;
    min-height: 40px;
    padding: 0 12px 0 8px;
    border: 0;
    background: transparent;
    font-size: 12px;
    font-weight: 500;
    color: var(--muted);
    text-align: left;
  }
  .sort-button span:first-child {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .sort-button.sorted {
    font-weight: 600;
    color: var(--text);
  }
  .arrow {
    flex: none;
    color: var(--border-strong);
  }
  .sorted .arrow {
    color: var(--accent);
  }
  .resize-handle {
    position: absolute;
    right: -5px;
    top: 6px;
    bottom: 6px;
    width: 10px;
    min-height: 0;
    padding: 0;
    border: 0;
    background: transparent;
    cursor: col-resize;
    z-index: 1;
    touch-action: none;
  }
  .resize-handle span {
    display: block;
    width: 1px;
    height: 100%;
    margin: 0 auto;
    background: var(--border-strong);
  }
  .resize-handle:hover:not(:disabled) {
    background: transparent;
  }
  .resize-handle:hover span,
  .resize-handle:focus-visible span {
    width: 2px;
    background: var(--accent);
  }
  .cell {
    padding: 9px 8px;
    min-width: 0;
    color: var(--body-text);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .file-name {
    display: block;
    font-weight: 500;
    color: var(--text);
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .file-folder {
    display: block;
    font-size: 12px;
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .grid-foot {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
    padding: 12px 8px;
    font-size: 13px;
  }
  .details {
    flex: 0 0 320px;
    min-width: 0;
    border-left: 1px solid var(--border);
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    font-size: 13px;
  }
  .details > div:first-child {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .details-name {
    font-size: 15px;
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  .details-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  @media (max-width: 760px) {
    .split {
      margin: 0 -16px;
    }
    .details {
      flex-basis: 100%;
      border-left: 0;
      border-top: 1px solid var(--border);
    }
  }
</style>
