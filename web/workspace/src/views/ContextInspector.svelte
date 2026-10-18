<!-- SPDX-License-Identifier: MIT -->
<!-- A saved attempt's measured usage, folds and original retained history. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import type { Json, ModelUsage, RunRecord } from '../lib/api';
  import type { ContextHistoryMessage } from '../lib/api-types';
  import { api, ApiError } from '../lib/api';
  import { contextUsage, plotX, plotY } from '../lib/contextUsage';
  import { focusPanel } from '../lib/focusPanel';
  import { runFolds } from '../lib/folds';
  import { formatTime } from '../lib/format';
  import { messageHref } from '../lib/router';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';

  let { run, runName, close }: { run: RunRecord; runName: string; close: () => void } = $props();
  let tab = $state<'timeline' | 'folds' | 'context' | 'history'>('timeline');
  let query = $state('');
  let role = $state('');
  let kind = $state('');
  let includeDerived = $state(false);
  let items = $state<ContextHistoryMessage[]>([]);
  let nextAfter = $state<number | null>(null);
  let coverage = $state('');
  let unsearchedCalls = $state(0);
  let loaded = $state(false);
  let loading = $state(false);
  let error = $state<unknown>(null);
  let serial = 0;
  const totalCalls = $derived(run.stored_history?.collections?.usage ?? run.usage?.length ?? 0);
  let usageRows = $state<ModelUsage[]>(untrack(() => run.usage ?? []));
  let usageLoading = $state(false);
  let usageError = $state<unknown>(null);
  const usage = $derived(contextUsage(usageRows));
  const summariesCount = $derived(run.stored_history?.collections?.summaries ?? 0);
  let foldsValue = $state<Json | undefined>(untrack(() => run.summaries?.folds));
  let foldsLoading = $state(false);
  let foldsLoaded = $state(untrack(() => summariesCount === 0));
  let foldsNext = $state<number | null>(untrack(() => (summariesCount ? 0 : null)));
  let foldsError = $state<unknown>(null);
  const folds = $derived(
    runFolds(run.context.project_id, { summaries: { folds: foldsValue ?? null } }),
  );
  type Filters = {
    contains: string;
    role?: 'user' | 'assistant' | 'tool';
    kind?: string;
    includeDerived: boolean;
  };
  let applied = $state<Filters>({ contains: '', includeDerived: false });

  async function loadUsage() {
    if (usageLoading || usageRows.length >= totalCalls) return;
    usageLoading = true;
    usageError = null;
    try {
      const page = await api.runHistory(
        run.context.project_id,
        run.context.session_id,
        runName,
        'usage',
        usageRows.length,
      );
      usageRows = [...usageRows, ...page.items.map((item) => item.value as ModelUsage)];
    } catch (caught) {
      usageError = caught;
    } finally {
      usageLoading = false;
    }
  }

  async function loadFolds() {
    if (foldsLoading || foldsLoaded || foldsNext === null) return;
    foldsLoading = true;
    foldsError = null;
    try {
      const inspect = (raw: unknown) => {
        const value = raw as { source?: string; value?: unknown };
        if (value.source === 'folds') {
          foldsValue = value.value as Json;
          foldsLoaded = true;
        }
      };
      const position = foldsNext;
      try {
        const page = await api.runHistory(
          run.context.project_id,
          run.context.session_id,
          runName,
          'summaries',
          position,
        );
        for (const item of page.items) inspect(item.value);
        foldsNext = page.next_offset;
      } catch (caught) {
        if (!(caught instanceof ApiError) || caught.kind !== 'unsupported') throw caught;
        const bytes: Uint8Array[] = [];
        let offset: number | null = 0;
        let total = 0;
        while (offset !== null) {
          const part = await api.runHistoryChunk(
            run.context.project_id,
            run.context.session_id,
            runName,
            'summaries',
            position,
            offset,
          );
          total = part.total;
          if (total > 2 * 1024 * 1024)
            throw new Error('Archived fold summary exceeds the 2 MiB inspector display limit.');
          bytes.push(
            Uint8Array.from(atob(part.data_base64), (character) => character.charCodeAt(0)),
          );
          offset = part.next_offset;
        }
        const joined = new Uint8Array(total);
        let cursor = 0;
        for (const part of bytes) {
          joined.set(part, cursor);
          cursor += part.length;
        }
        inspect(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(joined)));
        foldsNext = position + 1 < summariesCount ? position + 1 : null;
      }
      if (foldsNext === null) foldsLoaded = true;
    } catch (caught) {
      foldsError = caught;
    } finally {
      foldsLoading = false;
    }
  }

  if (untrack(() => usageRows.length < totalCalls)) void loadUsage();

  async function load(reset: boolean) {
    if (!reset && nextAfter === null) return;
    const ticket = ++serial;
    const after = reset ? 0 : (nextAfter ?? 0);
    if (reset) {
      applied = {
        contains: query.trim(),
        role: role ? (role as Filters['role']) : undefined,
        kind: kind.trim() || undefined,
        includeDerived,
      };
      items = [];
      nextAfter = null;
      coverage = '';
      unsearchedCalls = 0;
      loaded = false;
    }
    loading = true;
    error = null;
    try {
      const page = await api.contextHistory(
        run.context.project_id,
        run.context.session_id,
        runName,
        { after, ...applied },
      );
      if (ticket !== serial) return;
      items = reset ? page.messages : [...items, ...page.messages];
      nextAfter = page.next_after;
      coverage = page.coverage;
      unsearchedCalls += page.unsearched_oversized_tool_calls;
      loaded = true;
    } catch (caught) {
      if (ticket !== serial) return;
      error = caught;
    } finally {
      if (ticket === serial) loading = false;
    }
  }

  function choose(next: typeof tab) {
    tab = next;
    if (next === 'history' && !loaded) void load(true);
    if (next === 'folds' && !foldsLoaded) void loadFolds();
  }
</script>

<div class="context-backdrop">
  <div
    class="context-dialog"
    role="dialog"
    aria-modal="true"
    aria-labelledby="context-heading"
    tabindex="-1"
    use:focusPanel={{ active: true, close }}
  >
    <header class="context-header">
      <div>
        <span class="eyebrow">Agent diagnostics · saved attempt</span>
        <h2 id="context-heading">Context management</h2>
        <p class="muted">
          {run.context.project_id} / {run.context.session_id} · {run.role === 'delegated'
            ? 'Delegated agent'
            : 'Main agent'} · <code>{runName}</code>
        </p>
      </div>
      <button type="button" aria-label="Close context management" onclick={close}>Close</button>
    </header>
    <p class="notice">
      This is a read-only view of a saved attempt. It does not show live in-flight calls or merge
      separate continuation attempts.
    </p>
    <nav class="context-tabs" aria-label="Context sections">
      {#each ['timeline', 'folds', 'context', 'history'] as choice}
        <button
          type="button"
          class:active={tab === choice}
          aria-current={tab === choice ? 'page' : undefined}
          onclick={() => choose(choice as typeof tab)}
          >{choice === 'context'
            ? 'Current context'
            : choice === 'history'
              ? 'Original history'
              : choice.slice(0, 1).toUpperCase() + choice.slice(1)}</button
        >
      {/each}
    </nav>
    <div class="context-body">
      {#if tab === 'timeline'}
        <h3>Input context by answered model call</h3>
        <p class="muted">
          Horizontal position is call number, not time. Input tokens are provider-reported; missing
          calls remain gaps. Fold events cannot yet be placed at a measured call time.
        </p>
        {#if totalCalls > usageRows.length}<p class="muted">
            Loaded {usageRows.length} of {totalCalls} archived model calls. The graph and cache statistics
            cover the loaded prefix only.
          </p>
          <button type="button" disabled={usageLoading} onclick={() => void loadUsage()}
            >{usageLoading ? 'Loading usage…' : 'Load more usage'}</button
          >{/if}
        {#if usageError}<ErrorMessage error={usageError} />{/if}
        {#if usage.points.length}
          <svg
            class="usage-plot"
            viewBox="0 0 640 240"
            role="img"
            aria-label="Provider-reported input tokens by answered model call"
          >
            <path d="M 40 30 V 210 H 610" fill="none" stroke="currentColor" />
            {#each usage.segments as segment}
              {#if segment.length > 1}<polyline
                  points={segment
                    .map(
                      (point) =>
                        `${plotX(point.call, totalCalls)},${plotY(point.input, usage.maximum)}`,
                    )
                    .join(' ')}
                  fill="none"
                  stroke="var(--accent, #287364)"
                  stroke-width="3"
                />{/if}
              {#each segment as point}<circle
                  cx={plotX(point.call, totalCalls)}
                  cy={plotY(point.input, usage.maximum)}
                  r="4"
                  fill="var(--accent, #287364)"
                />{/each}
            {/each}
            <text x="42" y="23">{usage.maximum.toLocaleString()} input tokens</text>
            <text x="540" y="231">Call {totalCalls}</text>
          </svg>
          <p>
            Cache-read share: {usage.readShare === null
              ? 'unavailable'
              : `${(usage.readShare * 100).toFixed(1)}%`} · coverage {usage.cacheKnown} of {usage.eligible}
            calls with input counts. Reported cache reads {usage.cacheRead === null
              ? 'unavailable'
              : `${usage.cacheRead.toLocaleString()} tokens`}; reported cache writes {usage.cacheWrite ===
            null
              ? 'unavailable'
              : `${usage.cacheWrite.toLocaleString()} tokens`}.
          </p>
          <p class="muted">
            The share weights known calls by input tokens. Cache writes are separate from hits;
            cached tokens still occupy context. No configured ceiling or fold trigger is projected
            for this saved attempt.
          </p>
          <ol class="usage-calls">
            {#each usage.points as point}<li>
                Call {point.call}: {point.input.toLocaleString()} input, {point.output === null
                  ? 'output unavailable'
                  : `${point.output.toLocaleString()} output`}; cache read {point.read === null
                  ? 'unavailable'
                  : point.read.toLocaleString()}, write {point.write === null
                  ? 'unavailable'
                  : point.write.toLocaleString()}.
              </li>{/each}
          </ol>
        {:else if totalCalls > usageRows.length}<p>Archived usage is still loading.</p>
        {:else}<p>No provider input counts are saved for this attempt.</p>{/if}
      {:else if tab === 'folds'}
        <h3>Fold and summary records</h3>
        {#if summariesCount > 0 && !foldsLoaded}<p class="muted">
            Archived summaries are being searched for fold records. The inline preview may be
            incomplete.
          </p>
          <button
            type="button"
            disabled={foldsLoading || foldsNext === null}
            onclick={() => void loadFolds()}
            >{foldsLoading ? 'Loading summaries…' : 'Load more archived summaries'}</button
          >{/if}
        {#if foldsError}<ErrorMessage error={foldsError} />
          <p class="muted">
            The full fold summary remains archived; this view could not load it.
          </p>{/if}
        {#if folds && (folds.folds.length || folds.retired.length)}
          <p class="muted">
            These are saved transformation summaries. Their ordering is recorded, but this run
            record does not place them against individual model calls.
          </p>
          <ol>
            {#each folds.folds as fold}<li>
                {fold.text}{#if fold.href}
                  <a href={fold.href}>Open fold record</a>{/if}
              </li>{/each}
          </ol>
          {#if folds.retired.length}<h4>Retired markers</h4>
            <ul>
              {#each folds.retired as marker}<li>
                  {marker.text}: {marker.count} markers {#if marker.href}<a href={marker.href}
                      >Open record</a
                    >{/if}
                </li>{/each}
            </ul>{/if}
        {:else if !foldsLoaded}<p>Fold records in the archive have not been loaded yet.</p>
        {:else}<p>No fold summary is saved for this attempt.</p>{/if}
      {:else if tab === 'context'}
        <h3>Captured context composition</h3>
        <p>
          This run record keeps {run.messages.length} inline message previews{#if run.stored_history}
            and {run.stored_history.collections?.messages ?? 0} archived run messages{/if}. The
          original archive is available in the next tab.
        </p>
        <p class="muted">
          An exact last working-context snapshot and its section-by-section token counts were not
          captured here. Fold records show what was summarized, but this screen will not infer a
          precise final prompt from an inline preview or aggregate token usage.
        </p>
        {#if folds?.folds.length}<p>
            {folds.folds.length} fold or overview records are summarized under Folds.
          </p>{/if}
      {:else}
        <h3>Original retained messages</h3>
        <p class="muted">
          This server-side search reads the selected attempt's retained original text and tool-call
          arguments, not its folded working view. Search is an exact, case-sensitive substring.
        </p>
        <form
          class="history-filters"
          onsubmit={(event) => {
            event.preventDefault();
            void load(true);
          }}
        >
          <label>Search text <input type="search" bind:value={query} maxlength="128" /></label>
          <label
            >Role <select value={role} onchange={(event) => (role = event.currentTarget.value)}
              ><option value="">All</option><option value="user">User/platform</option><option
                value="assistant">Assistant</option
              ><option value="tool">Tool</option></select
            ></label
          >
          <label>Kind <input bind:value={kind} placeholder="Any kind" maxlength="32" /></label>
          <label class="check"
            ><input type="checkbox" bind:checked={includeDerived} /> Include derived fold/state records</label
          >
          <button type="submit">Search retained history</button>
        </form>
        {#if coverage}<p class="muted">{coverage}</p>{/if}
        {#if unsearchedCalls}<p class="muted">
            {unsearchedCalls} oversized tool-call field(s) in scanned messages were omitted from text
            search. Their messages remain available for direct inspection.
          </p>{/if}
        {#if loaded && (query.trim() !== applied.contains || role !== (applied.role ?? '') || kind.trim() !== (applied.kind ?? '') || includeDerived !== applied.includeDerived)}<p
            class="muted"
          >
            Filters changed. The results below still use the last submitted search; submit again to
            apply them.
          </p>{/if}
        {#if error}<ErrorMessage {error} />{/if}
        {#if loading && !loaded}<p>Loading original history…</p>{/if}
        {#if loaded}
          <ol class="history-results">
            {#each items as item (item.message)}<li>
                <div>
                  <strong>{visible(item.kind)}</strong> · {visible(item.role)} · {formatTime(
                    item.created,
                  )}
                  · position {item.position}
                </div>
                <p class="snippet">{visible(item.snippet)}</p>
                <a href={messageHref(run.context.project_id, item.message)}>Open original message</a
                >
              </li>{:else}<li>
                {nextAfter === null
                  ? 'No retained messages match this scope and query.'
                  : 'No matches in this scanned portion; continue the search.'}
              </li>{/each}
          </ol>
          {#if nextAfter !== null}<button
              type="button"
              disabled={loading}
              onclick={() => void load(false)}
              >{loading ? 'Loading…' : 'Continue search / more messages'}</button
            >{/if}
        {/if}
      {/if}
    </div>
  </div>
</div>

<style>
  .context-backdrop {
    position: fixed;
    inset: 0;
    z-index: 90;
    display: grid;
    place-items: center;
    background: #13231dcc;
    padding: 1rem;
  }
  .context-dialog {
    width: min(1120px, 100%);
    max-height: min(92vh, 960px);
    display: flex;
    flex-direction: column;
    background: var(--surface, #fff);
    color: var(--ink, #1b2925);
    border-radius: 0.8rem;
    box-shadow: 0 20px 70px #0006;
  }
  .context-header {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    align-items: start;
    padding: 1rem 1.2rem 0;
  }
  .context-header h2 {
    margin: 0.15rem 0;
  }
  .context-dialog > .notice {
    margin: 0.6rem 1.2rem;
  }
  .context-tabs {
    display: flex;
    gap: 0.25rem;
    padding: 0 1.2rem;
    border-bottom: 1px solid var(--border, #ddd);
  }
  .context-tabs button {
    border: 0;
    border-bottom: 2px solid transparent;
    background: transparent;
    padding: 0.7rem 0.5rem;
  }
  .context-tabs button.active {
    border-bottom-color: currentColor;
    font-weight: 700;
  }
  .context-body {
    overflow: auto;
    padding: 0.8rem 1.2rem 1.4rem;
  }
  .usage-plot {
    width: 100%;
    max-height: 320px;
    border: 1px solid var(--border, #ddd);
    border-radius: 0.4rem;
  }
  .usage-plot text {
    fill: currentColor;
    font-size: 12px;
  }
  .usage-calls {
    max-height: 11rem;
    overflow: auto;
  }
  .history-filters {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem;
    align-items: end;
  }
  .history-filters label {
    display: grid;
    gap: 0.2rem;
  }
  .history-filters .check {
    display: flex;
    align-items: center;
  }
  .history-results {
    padding-left: 1.5rem;
  }
  .history-results li {
    padding: 0.7rem 0;
    border-bottom: 1px solid var(--border, #ddd);
  }
  .snippet {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  @media (max-width: 700px) {
    .context-backdrop {
      padding: 0;
    }
    .context-dialog {
      width: 100%;
      max-height: 100vh;
      border-radius: 0;
    }
    .context-tabs {
      overflow-x: auto;
    }
  }
</style>
