<!-- SPDX-License-Identifier: MIT -->
<!-- Live, attempt-scoped observations; event times have different provenance. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type {
    ContextTimelinePage,
    TimelineAnswer,
    TimelineFold,
    TimelineAuxiliary,
  } from '../lib/api-types';
  import { contextUsage } from '../lib/contextUsage';
  import { formatTime } from '../lib/format';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { messageHref } from '../lib/router';
  import { visible } from '../lib/text';

  let { project, session, run }: { project: string; session: string; run: string } = $props();
  let answers = $state<TimelineAnswer[]>([]);
  let folds = $state<TimelineFold[]>([]);
  let auxiliary = $state<TimelineAuxiliary[]>([]);
  let answersAfter = 0;
  let eventsAfter = 0;
  let moreAnswers = $state(false);
  let moreEvents = $state(false);
  let moreAuxiliary = $state(false);
  let loaded = $state(false);
  let poll = $state<PollState<ContextTimelinePage>>(initialPoll());
  let coverage = $state('');
  let owner: Poller<ContextTimelinePage> | null = null;
  const usage = $derived(contextUsage(answers.map((item) => item.usage)));
  const times = $derived(
    [
      ...answers.map((item) => Date.parse(item.observed_at)),
      ...folds.map((item) => Date.parse(item.at)),
    ].filter((value) => Number.isFinite(value)),
  );
  const firstTime = $derived(times.length ? Math.min(...times) : 0);
  const lastTime = $derived(times.length ? Math.max(...times) : 0);
  const maximum = $derived(Math.max(1, ...answers.map((item) => item.usage.input_tokens ?? 0)));

  function x(at: string): number {
    const time = Date.parse(at);
    if (!Number.isFinite(time)) return 40;
    return lastTime === firstTime ? 325 : 40 + ((time - firstTime) / (lastTime - firstTime)) * 570;
  }

  function y(input: number): number {
    return 210 - (input / maximum) * 170;
  }

  $effect(() => {
    const attemptProject = project,
      attemptSession = session,
      attemptRun = run;
    answers = [];
    folds = [];
    auxiliary = [];
    answersAfter = 0;
    eventsAfter = 0;
    moreAnswers = false;
    moreEvents = false;
    moreAuxiliary = false;
    coverage = '';
    loaded = false;
    poll = initialPoll();
    let lastPage: ContextTimelinePage | null = null;
    let tail = true;
    const current = new Poller(
      (signal) =>
        api.contextTimeline(
          attemptProject,
          attemptSession,
          attemptRun,
          answersAfter,
          eventsAfter,
          signal,
          tail,
        ),
      (state) => {
        poll = state;
        if (state.phase === 'signed_out') {
          answers = [];
          folds = [];
          auxiliary = [];
          loaded = false;
        }
        if (state.phase !== 'observed' || !state.data || state.data === lastPage) return;
        const page = state.data;
        lastPage = page;
        tail = false;
        // Only the visible window is retained; cursors still advance through
        // the entire attempt. Each read is one bounded server page.
        answers = [...answers, ...page.answers].slice(-500);
        folds = [...folds, ...page.folds].slice(-500);
        auxiliary = page.auxiliary;
        answersAfter = page.answers_after;
        eventsAfter = page.events_after;
        moreAnswers = page.more_answers;
        moreEvents = page.more_events;
        moreAuxiliary = page.more_auxiliary;
        coverage = page.coverage;
        loaded = true;
      },
      { intervalMs: 5000, deadlineMs: 15000 },
    );
    owner = current;
    const stop = current.watch();
    return () => {
      stop();
      if (owner === current) owner = null;
    };
  });

  function refresh() {
    if (owner === null || poll.busy) return;
    owner.setVisible(false);
    owner.setVisible(true);
  }
</script>

<section aria-label="Live attempt context timeline">
  <h3>Attempt timeline</h3>
  <p class="muted">
    Starts with the latest 50 answers and event rows, then follows new observations. Earlier
    conversation text remains in Full history. Answer points use the time their response was saved,
    not request start. Fold marks use event emission time. Input counts are provider-reported; fold
    reductions and view sizes are estimates.
  </p>
  {#if coverage}<p class="muted">{coverage}</p>{/if}
  {#if poll.phase === 'unavailable' || poll.phase === 'unsupported'}
    <p role="alert">Attempt observations are temporarily unavailable. Refresh will retry.</p>
  {:else if poll.phase === 'missing' || poll.phase === 'changed'}
    <p role="alert">This attempt is no longer available here.</p>
  {:else if poll.phase === 'signed_out'}
    <p role="alert">Sign in again to view attempt observations.</p>
  {/if}
  {#if poll.busy && !loaded}<p>Loading attempt observations…</p>{/if}
  {#if loaded}
    <p class="muted">
      Displaying {answers.length} answer observations and {folds.length} fold events, at most 500 of each.
      {#if moreAnswers || moreEvents}More recorded rows remain.{:else}The current cursor is caught
        up. This page checks for new rows while visible.{/if}
    </p>
    {#if times.length}
      <svg
        class="usage-plot"
        viewBox="0 0 640 240"
        role="img"
        aria-label="Provider-reported input tokens at response-observation time with timestamped fold events"
      >
        <path d="M 40 30 V 210 H 610" fill="none" stroke="currentColor" />
        {#each folds as event (event.seq)}
          <path
            d={`M ${x(event.at)} 35 V 210`}
            fill="none"
            stroke="var(--warning, #a35a25)"
            stroke-width="2"
            stroke-dasharray="4 4"
          />
        {/each}
        {#each answers as answer (answer.message)}
          {#if answer.usage.input_tokens !== null}
            <circle
              cx={x(answer.observed_at)}
              cy={y(answer.usage.input_tokens)}
              r="4"
              fill="var(--accent, #287364)"
            />
          {/if}
        {/each}
        <text x="42" y="23">{maximum.toLocaleString()} input tokens</text>
        <text x="42" y="231">{formatTime(new Date(firstTime).toISOString())}</text>
        <text x="490" y="231">{formatTime(new Date(lastTime).toISOString())}</text>
      </svg>
    {/if}
    <p>
      Cache-read share: {usage.readShare === null
        ? 'unavailable'
        : `${(usage.readShare * 100).toFixed(1)}%`} · coverage {usage.cacheKnown} of
      {usage.eligible} answers with input counts. Cache writes are separate from hits; unknown counters
      are unavailable, not zero.
    </p>
    <h4>Answer observations</h4>
    <ol class="timeline-list">
      {#each answers as answer (answer.message)}
        <li>
          <a href={messageHref(project, answer.message)}>Call {answer.ordinal}</a> · response
          observed
          {formatTime(answer.observed_at)} · {answer.usage.input_tokens === null
            ? 'input tokens unavailable'
            : `${answer.usage.input_tokens.toLocaleString()} input tokens`} · cache read
          {answer.usage.cache_read_input_tokens === null
            ? 'unavailable'
            : answer.usage.cache_read_input_tokens.toLocaleString()}
        </li>
      {:else}
        <li>
          No journaled answer observations at this cursor. Older saved runs may have only a
          call-number record.
        </li>
      {/each}
    </ol>
    <h4>Fold events</h4>
    <ol class="timeline-list">
      {#each folds as event (event.seq)}
        <li>
          {formatTime(event.at)} · {visible(event.state ?? 'fold event')}
          {#if event.span_start !== null && event.span_end !== null}
            · original positions {event.span_start}–{event.span_end}
          {/if}
          {#if event.freed_estimate !== null}
            · estimated freed {event.freed_estimate} tokens{/if}
          {#if event.tokens_estimate !== null}
            · estimated view {event.tokens_estimate} tokens{/if}
          {#if event.fold}<a href={messageHref(project, event.fold)}>Open fold record</a>{/if}
        </li>
      {:else}<li>No fold event in the loaded window.</li>{/each}
    </ol>
    <h4>Other model calls</h4>
    <p class="muted">
      Latest 50 auxiliary intents at most, refreshed as a snapshot. Intent creation is not response
      observation time; an older intent may settle later. These calls are separate from the agent's
      input-context series.{#if moreAuxiliary}
        Older auxiliary intents are outside this snapshot.{/if}
    </p>
    <ol class="timeline-list">
      {#each auxiliary as call (call.identity)}
        <li>
          {visible(call.kind)} · intent created {formatTime(call.intent_at)} · {call.usage === null
            ? 'usage unavailable or unresolved'
            : call.usage.input_tokens === null
              ? 'observed response, input tokens unavailable'
              : `observed ${call.usage.input_tokens.toLocaleString()} input tokens`}
          {#if call.usage}
            · cache read {call.usage.cache_read_input_tokens == null
              ? 'unavailable'
              : call.usage.cache_read_input_tokens.toLocaleString()}
            · cache write {call.usage.cache_creation_input_tokens == null
              ? 'unavailable'
              : call.usage.cache_creation_input_tokens.toLocaleString()}
            · cost {call.usage.cost == null
              ? 'unavailable'
              : `$${call.usage.cost.toFixed(4)} (${call.usage.cost_source ?? 'source unavailable'})`}
          {/if}
        </li>
      {:else}<li>No auxiliary model intents in the latest snapshot.</li>{/each}
    </ol>
  {/if}
  <button type="button" disabled={poll.busy || poll.phase === 'signed_out'} onclick={refresh}
    >{poll.busy
      ? 'Loading observations…'
      : moreAnswers || moreEvents
        ? 'Load next page'
        : 'Refresh observations'}</button
  >
</section>

<style>
  .usage-plot {
    width: 100%;
    max-height: 320px;
    border: 1px solid var(--border, #ddd);
    border-radius: 0.4rem;
  }
  .usage-plot text {
    fill: currentColor;
    font-size: 11px;
  }
  .timeline-list {
    max-height: 14rem;
    overflow: auto;
    padding-left: 1.5rem;
  }
  .timeline-list li {
    padding: 0.25rem 0;
  }
</style>
