<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import {
    reviews,
    type Review,
    type Finding,
    type Evidence,
    type MathJob,
    type MathQueue,
  } from '../lib/reviews';
  import type { Json } from '../lib/api';
  import FindingCard from './FindingCard.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  import PinnedEvidencePreview from './PinnedEvidencePreview.svelte';
  let { projectId, sessionId }: { projectId: string; sessionId: string } = $props();
  let listed = $state<Review[]>([]),
    findings = $state<Finding[]>([]),
    nextReview = $state<string | null>(null),
    nextFinding = $state<string | null>(null);
  let unreviewed = $state<Json[]>([]),
    selected = $state<Review | null>(null),
    evidence = $state<Evidence[]>([]),
    nextEvidence = $state<number | null>(null);
  let output = $state(''),
    outputReview = $state(''),
    outputId = $state(''),
    nextOutput = $state<number | null>(null);
  let records = $state<Json[]>([]),
    nextRecord = $state<number | null>(null),
    busy = $state(false),
    error = $state<unknown>(null),
    notice = $state('');
  let checks = $state<MathJob[]>([]),
    queue = $state<MathQueue[]>([]),
    nextCheck = $state<string | null>(null),
    queueMore = $state(false);
  let mathOutput = $state(''),
    mathId = $state(''),
    mathNext = $state<number | null>(null);
  let selection = 0,
    readGeneration = 0,
    mathGeneration = 0;
  async function refresh() {
    busy = true;
    error = null;
    try {
      const [r, f, m] = await Promise.all([
        reviews.list(projectId, sessionId),
        reviews.findings(projectId, sessionId),
        reviews.math(projectId, sessionId),
      ]);
      checks = m.checks;
      queue = m.queue;
      nextCheck = m.next_after;
      queueMore = !!m.queue_more;
      listed = r.reviews;
      if (selected)
        selected = listed.find((item) => item.review_id === selected?.review_id) ?? selected;
      nextReview = r.next_after;
      unreviewed = r.unreviewed;
      findings = f.findings;
      nextFinding = f.next_after;
    } catch (caught) {
      error = caught;
    } finally {
      busy = false;
    }
  }
  $effect(() => {
    projectId;
    sessionId;
    void refresh();
  });
  async function more(kind: 'reviews' | 'findings') {
    busy = true;
    try {
      if (kind === 'reviews') {
        const result = await reviews.list(projectId, sessionId, nextReview);
        listed = [...listed, ...result.reviews];
        nextReview = result.next_after;
      } else {
        const result = await reviews.findings(projectId, sessionId, nextFinding);
        findings = [...findings, ...result.findings];
        nextFinding = result.next_after;
      }
    } catch (caught) {
      error = caught;
    } finally {
      busy = false;
    }
  }
  async function select(review: Review) {
    const generation = ++selection;
    readGeneration++;
    busy = true;
    selected = review;
    evidence = [];
    records = [];
    output = '';
    outputId = '';
    outputReview = '';
    nextEvidence = null;
    nextRecord = null;
    nextOutput = null;
    try {
      const result = await reviews.manifest(projectId, sessionId, review.review_id);
      if (generation !== selection) return;
      selected = result.review;
      evidence = result.evidence;
      nextEvidence = result.next_after;
      nextRecord = 0;
    } catch (caught) {
      if (generation === selection) error = caught;
    } finally {
      if (generation === selection) busy = false;
    }
  }
  async function moreEvidence() {
    if (busy || !selected || nextEvidence === null) return;
    const generation = selection,
      id = selected.review_id,
      cursor = nextEvidence;
    busy = true;
    try {
      const result = await reviews.manifest(projectId, sessionId, id, cursor);
      if (generation !== selection) return;
      evidence = [...evidence, ...result.evidence];
      nextEvidence = result.next_after;
    } catch (caught) {
      if (generation === selection) error = caught;
    } finally {
      if (generation === selection) busy = false;
    }
  }
  async function read(review: string, id: string, start = 0) {
    if (start && (review !== outputReview || id !== outputId)) return;
    const generation = ++readGeneration,
      previous = output;
    try {
      const result = await reviews.evidence(projectId, sessionId, review, id, start);
      if (generation !== readGeneration) return;
      output = start ? previous + result.text : result.text;
      outputReview = review;
      outputId = id;
      nextOutput = result.next_start;
    } catch (caught) {
      if (generation === readGeneration) error = caught;
    }
  }
  async function audit() {
    if (busy || !selected || nextRecord === null) return;
    const generation = selection,
      id = selected.review_id,
      cursor = nextRecord;
    busy = true;
    try {
      const result = await reviews.records(projectId, sessionId, id, cursor);
      if (generation !== selection) return;
      records = [...records, ...result.records.map((item) => item as unknown as Json)];
      nextRecord = result.next_after;
    } catch (caught) {
      if (generation === selection) error = caught;
    } finally {
      if (generation === selection) busy = false;
    }
  }
  async function moreChecks() {
    try {
      const m = await reviews.math(projectId, sessionId, nextCheck);
      checks = [...checks, ...m.checks];
      nextCheck = m.next_after;
    } catch (caught) {
      error = caught;
    }
  }
  async function mathEvidence(id: string, after = 0) {
    const generation = ++mathGeneration,
      previous = mathOutput;
    try {
      const result = await reviews.mathEvidence(projectId, sessionId, id, after);
      if (generation !== mathGeneration) return;
      mathOutput = after
        ? previous + '\n' + JSON.stringify(result.observations, null, 2)
        : JSON.stringify(result, null, 2);
      mathId = id;
      mathNext = result.next_after;
    } catch (caught) {
      if (generation === mathGeneration) error = caught;
    }
  }
  async function request() {
    try {
      notice = (await reviews.request(projectId, sessionId)).detail;
    } catch (caught) {
      error = caught;
    }
  }
</script>

<section aria-label="Consistency reviews">
  <h2>Consistency reviews</h2>
  <p class="muted">
    A pass covers the recorded checks and is not a proof. Missing evidence, capped reviews and
    unconfirmed fixes remain explicit.
  </p>
  <button disabled={busy} onclick={refresh}>Refresh reviews</button><button
    disabled={busy}
    onclick={request}>Request review at next live safe point</button
  >
  {#if notice}<p role="status">{notice}</p>{/if}
  {#if unreviewed.length}<details open>
      <summary>Windows left unreviewed after bounded retries</summary>
      <pre>{JSON.stringify(unreviewed, null, 2)}</pre>
    </details>{/if}
  {#if !listed.length}<p>No consistency reviews recorded.</p>{/if}
  <ul>
    {#each listed as review (review.review_id)}<li>
        <button onclick={() => select(review)}
          >{review.kind} · {review.state} · {new Date(
            review.created * 1000,
          ).toLocaleString()}</button
        >
        {review.summary}
      </li>{/each}
  </ul>
  {#if nextReview}<button disabled={busy} onclick={() => more('reviews')}>Load more reviews</button
    >{/if}
  {#if selected}
    <h3>{selected.kind} review · {selected.state}</h3>
    <p class="prose">{selected.summary}</p>
    <p>
      Alias {selected.alias ?? 'unavailable'} · model {selected.resolved_model ?? 'unavailable'} · {selected.calls}
      model calls · {selected.input_tokens} input / {selected.output_tokens} output tokens · {selected.estimated_tokens}
      reserved estimate.
    </p>
    <p>
      {#if selected.kind === 'confirmation'}Pinned finding and fix scope · no author feed consumed{:else}Feed
        {selected.start_seq}–{selected.end_seq}{/if} · coverage {selected.coverage_complete
        ? 'complete within available evidence'
        : 'incomplete'} · cap {selected.cap_reached ? 'reached' : 'not reached'} · rubric {selected.rubric}.
    </p>
    <details>
      <summary>Classifier and configuration identity</summary>
      <pre>{JSON.stringify(
          { classifier: selected.classifier, config_revision: selected.config_revision },
          null,
          2,
        )}</pre>
    </details>
    <ul>
      {#each evidence as item (item.evidence)}<li>
          <button onclick={() => read(selected!.review_id, item.evidence)}
            >{item.kind} · {item.complete ? 'available' : 'incomplete'}</button
          ><code>{JSON.stringify(item.locator)}</code>
        </li>{/each}
    </ul>
    {#if nextEvidence !== null}<button disabled={busy} onclick={moreEvidence}
        >Load more snapshot evidence</button
      >{/if}
    <button disabled={busy || nextRecord === null} onclick={audit}>Load review audit records</button
    >
    {#each records as record, index (index)}<details>
        <summary>Audit record {index + 1}</summary>
        <pre>{JSON.stringify(record, null, 2)}</pre>
      </details>{/each}
  {/if}
  {#if outputId}<PinnedEvidencePreview text={output} />{#if nextOutput !== null}<button
        onclick={() => read(outputReview, outputId, nextOutput!)}>Read next evidence slice</button
      >{/if}{/if}
  <h3>Mechanical math checks</h3>
  <p class="muted">
    Mechanical checks use the operator-configured renderer. Unavailable, queued and quota-held work
    cannot establish a fix. Checks with no eligible author remain visible here.
  </p>
  {#if queue.length}<details open>
      <summary>Pending or held source revisions</summary>
      <pre>{JSON.stringify(queue, null, 2)}</pre>
      {#if queueMore}<p>Additional sources remain queued or held.</p>{/if}
    </details>{/if}
  {#if !checks.length}<p>No mechanical math checks recorded.</p>{/if}
  <ul>
    {#each checks as check (check.check_id)}<li>
        <button onclick={() => mathEvidence(check.check_id)}
          >{check.kind === 'comment'
            ? 'Comment body'
            : check.kind === 'response'
              ? 'Finding explanation'
              : 'Math'} revision {check.revision} · {check.state} · {check.rejected} rejected · {check.unavailable}
          unavailable</button
        >{#if check.macro_context_status}<p class="muted">
            Project macro revision {check.project_macro_revision} · context {check.macro_context_status}.
          </p>{/if}{#if check.document}<code>{check.document}</code>{/if}{#if check.comment}<p
            class="muted"
          >
            Original entry <code>{check.comment.entry_id}</code>; document macro revision {check
              .comment.macro_revision}.
          </p>{/if}{#if check.response}<p class="muted">
            Finding <code>{check.response.finding}</code>; original response {check.response
              .response_revision}; pinned source macro context.
          </p>{/if}
      </li>{/each}
  </ul>
  {#if nextCheck}<button onclick={moreChecks}>Load more math checks</button>{/if}
  {#if mathOutput}<h4>Mechanical evidence and author provenance</h4>
    <pre aria-label="Document math check">{mathOutput}</pre>
    {#if mathNext !== null}<button onclick={() => mathEvidence(mathId, mathNext!)}
        >Read next math evidence page</button
      >{/if}{/if}
  <h3>Findings</h3>
  {#if !findings.length}<p>No consistency findings recorded.</p>{/if}
  {#each findings as finding (finding.finding_id)}<FindingCard
      {projectId}
      {sessionId}
      {finding}
      changed={refresh}
      {read}
    />{/each}
  {#if nextFinding}<button disabled={busy} onclick={() => more('findings')}
      >Load more findings</button
    >{/if}
  {#if error}<ErrorMessage {error} />{/if}
</section>

<style>
  pre,
  .prose {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  pre {
    max-height: 22rem;
    overflow: auto;
  }
  code {
    overflow-wrap: anywhere;
  }
  button {
    margin: 0.25rem;
  }
  section {
    border-top: 1px solid var(--border, #9996);
    margin-top: 1rem;
  }
</style>
