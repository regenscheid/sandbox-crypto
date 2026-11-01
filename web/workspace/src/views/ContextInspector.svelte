<!-- SPDX-License-Identifier: MIT -->
<!-- A saved attempt's measured usage, folds and original retained history. -->
<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
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
  import ContextAttempts from './ContextAttempts.svelte';
  import ContextTimeline from './ContextTimeline.svelte';
  import ContextComposition from './ContextComposition.svelte';

  let {
    run,
    runName,
    close,
    project,
    session,
    initialTab = 'timeline',
    initialScope = 'attempt',
  }: {
    run: RunRecord | null;
    runName: string;
    close: () => void;
    project?: string;
    session?: string;
    /** The section it opens on. */
    initialTab?: 'timeline' | 'folds' | 'context' | 'history';
    /** This attempt alone, or the agent across its continuations, as the main agent's is. */
    initialScope?: 'attempt' | 'agent';
  } = $props();
  const projectId = $derived(run?.context.project_id ?? project ?? '');
  const sessionId = $derived(run?.context.session_id ?? session ?? '');
  let tab = $state<'timeline' | 'folds' | 'context' | 'history'>(untrack(() => initialTab));
  let query = $state('');
  let role = $state('');
  let kind = $state('');
  let includeDerived = $state(false);
  let scope = $state<'attempt' | 'agent'>(untrack(() => initialScope));
  let scanning = $state(false);
  let searchGeneration = 0;
  let requestController: AbortController | null = null;
  let scanPause = $state('');
  let matched = $state(0);
  onDestroy(stopSearch);
  let items = $state<ContextHistoryMessage[]>([]);
  let nextAfter = $state<number | null>(null);
  let coverage = $state('');
  let unsearchedCalls = $state(0);
  let indexPending = $state(false);
  let indexing = $state(false);
  let loaded = $state(false);
  let loading = $state(false);
  let error = $state<unknown>(null);
  let serial = 0;
  const totalCalls = $derived(run?.stored_history?.collections?.usage ?? run?.usage?.length ?? 0);
  let usageRows = $state<ModelUsage[]>(untrack(() => run?.usage ?? []));
  let usageLoading = $state(false);
  let usageError = $state<unknown>(null);
  const usage = $derived(contextUsage(usageRows));
  const summariesCount = $derived(run?.stored_history?.collections?.summaries ?? 0);
  let foldsValue = $state<Json | undefined>(untrack(() => run?.summaries?.folds));
  let foldsLoading = $state(false);
  let foldsLoaded = $state(untrack(() => summariesCount === 0));
  let foldsNext = $state<number | null>(untrack(() => (summariesCount ? 0 : null)));
  let foldsError = $state<unknown>(null);
  const folds = $derived(runFolds(projectId, { summaries: { folds: foldsValue ?? null } }));
  type Filters = {
    contains: string;
    role?: 'user' | 'assistant' | 'tool';
    kind?: string;
    includeDerived: boolean;
    scope: 'attempt' | 'agent';
  };
  let applied = $state<Filters>({
    contains: '',
    includeDerived: false,
    scope: untrack(() => initialScope),
  });

  async function loadUsage() {
    if (usageLoading || usageRows.length >= totalCalls) return;
    usageLoading = true;
    usageError = null;
    try {
      const page = await api.runHistory(projectId, sessionId, runName, 'usage', usageRows.length);
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
        const page = await api.runHistory(projectId, sessionId, runName, 'summaries', position);
        for (const item of page.items) inspect(item.value);
        foldsNext = page.next_offset;
      } catch (caught) {
        if (!(caught instanceof ApiError) || caught.kind !== 'unsupported') throw caught;
        const bytes: Uint8Array[] = [];
        let offset: number | null = 0;
        let total = 0;
        while (offset !== null) {
          const part = await api.runHistoryChunk(
            projectId,
            sessionId,
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

  async function load(reset: boolean): Promise<boolean> {
    if (!reset && nextAfter === null) return false;
    const ticket = ++serial;
    requestController?.abort();
    const controller = new AbortController();
    requestController = controller;
    const after = reset ? 0 : (nextAfter ?? 0);
    if (reset) {
      applied = {
        contains: query,
        role: role ? (role as Filters['role']) : undefined,
        kind: kind.trim() || undefined,
        includeDerived,
        scope,
      };
      items = [];
      matched = 0;
      nextAfter = null;
      coverage = '';
      unsearchedCalls = 0;
      loaded = false;
    }
    loading = true;
    error = null;
    try {
      const page = await api.contextHistory(projectId, sessionId, runName, {
        after,
        ...applied,
        signal: controller.signal,
      });
      if (ticket !== serial) return false;
      matched += page.messages.length;
      items = (reset ? page.messages : [...items, ...page.messages]).slice(-200);
      nextAfter = page.next_after;
      coverage = page.coverage;
      unsearchedCalls += page.unsearched_oversized_tool_calls;
      indexPending = page.index_pending;
      loaded = true;
      return true;
    } catch (caught) {
      if (ticket !== serial) return false;
      error = caught;
      return false;
    } finally {
      if (ticket === serial) loading = false;
    }
  }

  async function indexBatch() {
    indexing = true;
    try {
      const progress = await api.indexContextHistory(projectId, sessionId, runName, applied.scope);
      indexPending = progress.pending;
      await load(true);
    } catch (caught) {
      error = caught;
    } finally {
      indexing = false;
    }
  }

  function stopSearch() {
    requestController?.abort();
    requestController = null;
    serial++;
    searchGeneration++;
    scanning = false;
    loading = false;
  }

  async function searchAll(reset = true) {
    stopSearch();
    scanning = true;
    scanPause = '';
    const generation = searchGeneration;
    const initialMatches = reset ? 0 : matched;
    let pages = 1;
    let ok = await load(reset);
    while (
      generation === searchGeneration &&
      scanning &&
      ok &&
      nextAfter !== null &&
      matched - initialMatches < 200 &&
      pages < 100
    ) {
      pages++;
      ok = await load(false);
    }
    if (generation === searchGeneration) {
      scanning = false;
      if (ok && nextAfter !== null)
        scanPause = `Search paused after ${pages} pages or 200 new matches. Continue from the saved cursor.`;
    }
  }

  function choose(next: typeof tab) {
    if (next !== tab) stopSearch();
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
        <span class="eyebrow">Agent diagnostics · {run ? 'saved' : 'live or unsaved'} attempt</span>
        <h2 id="context-heading">Context management</h2>
        <p class="muted">
          {projectId} / {sessionId} · {run?.role === 'delegated'
            ? 'Delegated agent'
            : run?.role === 'main'
              ? 'Main agent'
              : 'Agent'} · <code>{runName}</code>
        </p>
      </div>
      <button type="button" aria-label="Close context management" onclick={close}>Close</button>
    </header>
    <p class="notice">
      The timeline reads this attempt's recorded observations while it runs. An in-flight model call
      has no answer observation yet. Original history can also search this agent's recorded
      continuation attempts.
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
        <ContextTimeline project={projectId} session={sessionId} run={runName} />
        {#if run}<h3>Saved call-number record</h3>
          <p class="muted">
            This archived fallback uses call number, not time. It may overlap the journaled answer
            observations above; use it for legacy calls without timestamps.
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
          {:else}<p>No provider input counts are saved in the run record.</p>{/if}{/if}
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
        {:else}<p>
            No fold summary is saved for this attempt. Recorded fold events appear on the timeline.
          </p>{/if}
      {:else if tab === 'context'}
        <ContextComposition project={projectId} session={sessionId} run={runName} {initialScope} />
      {:else}
        <h3>Original retained messages</h3>
        <ContextAttempts project={projectId} session={sessionId} run={runName} {close} />
        <p class="muted">
          This server-side search reads retained original text and tool-call arguments in the
          selected scope, unaffected by folding. Search is an exact, case-sensitive substring.
        </p>
        <form
          class="history-filters"
          onsubmit={(event) => {
            event.preventDefault();
            stopSearch();
            void load(true);
          }}
        >
          <label
            >History scope <select
              value={scope}
              onchange={(event) => (scope = event.currentTarget.value as typeof scope)}
            >
              <option value="attempt">Selected attempt</option>
              <option value="agent">This agent across continuations</option>
            </select></label
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
          <button type="button" disabled={scanning} onclick={() => void searchAll()}
            >Search all pages</button
          >
          {#if scanning || loading}<button type="button" onclick={stopSearch}>Stop search</button
            >{/if}
        </form>
        {#if scanPause}<p class="notice">{scanPause}</p>{/if}
        {#if loaded && nextAfter !== null && !loading}<button
            type="button"
            onclick={() => void searchAll(false)}>Continue automatic search</button
          >{/if}
        {#if coverage}<p class="muted">{coverage}</p>{/if}
        {#if indexPending}<p class="notice">
            Some original messages are not indexed yet. These results are partial; a miss is not a
            complete negative result.
          </p>
          <button type="button" disabled={indexing || loading} onclick={() => void indexBatch()}
            >{indexing ? 'Indexing…' : 'Index next batch of original text'}</button
          >
        {/if}
        {#if unsearchedCalls}<p class="muted">
            {unsearchedCalls} oversized tool-call field(s) in scanned messages were omitted from text
            search. Their messages remain available for direct inspection.
          </p>{/if}
        {#if loaded && (query !== applied.contains || role !== (applied.role ?? '') || kind.trim() !== (applied.kind ?? '') || includeDerived !== applied.includeDerived || scope !== applied.scope)}<p
            class="muted"
          >
            Filters changed. The results below still use the last submitted search; submit again to
            apply them.
          </p>{/if}
        {#if error}<ErrorMessage {error} />{/if}
        {#if loading && !loaded}<p>Loading original history…</p>{/if}
        {#if loaded}
          <p class="muted">
            {matched} matching messages found{nextAfter !== null
              ? ' so far; more archive remains'
              : indexPending
                ? '; indexed portion complete'
                : '; scan complete'}. {#if matched > items.length}Showing the latest {items.length} results
              to bound memory.{/if}
          </p>
          <ol class="history-results">
            {#each items as item (item.message)}<li>
                <div>
                  <strong>{visible(item.kind)}</strong> · {visible(item.role)} · {formatTime(
                    item.created,
                  )}
                  · position {item.position} · attempt <code>{item.run}</code>
                </div>
                <p class="snippet">{visible(item.snippet)}</p>
                <a href={messageHref(projectId, item.message)}>Open original message</a>
              </li>{:else}<li>
                {nextAfter === null
                  ? indexPending
                    ? 'No matches in indexed text; original messages remain to index.'
                    : 'No retained messages match this scope and query.'
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
