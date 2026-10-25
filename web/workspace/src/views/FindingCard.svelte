<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { reviews, type Finding, type Review, type Transition } from '../lib/reviews';
  import { Detail, noDetail } from '../lib/detail';
  import { findingDrafts } from '../lib/drafts';
  import { Earlier } from '../lib/earlier';
  import { afterResponse, findingHistory, findingResponse, startResponse } from '../lib/findings';
  import {
    anchorView,
    decisions,
    dismissalText,
    raisedText,
    recheckText,
    severityText,
    statusClass,
    statusLabel,
    statusText,
    successorText,
  } from '../lib/findingView';
  import { guardedWrite } from '../lib/mutation';
  import { emptyList } from '../lib/pagedList';
  import { idle, submit } from '../lib/submit';
  import { EvidenceText } from '../lib/evidenceText';
  import { visible } from '../lib/text';
  import ApprovalText from './ApprovalText.svelte';
  import CopiedNumbers from './CopiedNumbers.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  import FindingTransition from './FindingTransition.svelte';
  import HiddenBanner from './HiddenBanner.svelte';
  import LatestExplanation from './LatestExplanation.svelte';
  import PinnedEvidencePreview from './PinnedEvidencePreview.svelte';
  import Markdown from '../lib/Markdown.svelte';
  import { macroSet } from '../lib/macros';
  import { copiedValues } from '../lib/copiedNumbers';
  import { reviewHref } from '../lib/router';
  let {
    projectId,
    sessionId,
    finding,
    review = undefined,
    folded = false,
    changed,
  }: {
    projectId: string;
    sessionId: string;
    finding: Finding;
    /** The review that raised it, when the panel has it loaded (CR-17). */
    review?: Review | undefined;
    /** Shown closed, for a resolved finding; the card keeps its state either way. */
    folded?: boolean;
    changed: () => Promise<void>;
  } = $props();
  const initial = untrack(() => finding);
  const macros = $derived(
    macroSet(
      finding.macro_context?.macros ?? {},
      `${projectId}:${finding.macro_context?.key ?? 'unknown-finding'}`,
    ),
  );
  const key = untrack(() => `${projectId}:${sessionId}:${initial.finding_id}`);
  let draft = $state(startResponse(initial, findingDrafts.load(key).draft));
  let persisted = $state(true);
  const allowed = $derived(decisions(finding.status));
  const anchors = $derived(finding.anchors.map((anchor) => anchorView(projectId, anchor)));
  /** A copied-number finding's two numbers, which the card lines up digit by digit. */
  const copied = $derived(copiedValues(finding));
  // A kept draft's decision is never rewritten from the status: one the status no longer
  // allows stays shown, marked, and cannot be saved until the user chooses again.
  const allowedNow = $derived(allowed.some((item) => item.action === draft.action));
  const responding = $state(idle());
  const pages = $state(emptyList<Transition[], number>());
  const history = new Earlier(
    pages,
    findingHistory((before) => reviews.finding(projectId, sessionId, finding.finding_id, before)),
  );
  // Pinned evidence a reviewer anchored the finding to, read here, slice by slice.
  const slices = $state(emptyList<string, number>());
  const pinned = new EvidenceText(slices, (review, item, start) =>
    reviews.evidence(projectId, sessionId, review, item, start),
  );
  // Mechanical evidence of one check (and formula), keyed by both.
  const evidence = $state(noDetail<string, string>());
  const checks = new Detail(evidence, async (shown) => {
    const [check, span] = shown.split('\u0000');
    const result = await reviews.mathEvidence(projectId, sessionId, check!, 0, span || undefined);
    return JSON.stringify(result, null, 2);
  });
  const busy = $derived(responding.busy || pages.loading);
  let historyHead = initial.revision,
    sourceHead = initial.response_generation ?? 0;
  $effect(() => {
    // A new finding revision, or new response bodies, start the history again and drop
    // evidence read for the revision before.
    const revision = finding.revision,
      generation = finding.response_generation ?? 0;
    if (revision !== historyHead) checks.clear();
    if (revision !== historyHead || generation !== sourceHead) {
      history.reset();
      historyHead = revision;
      sourceHead = generation;
    }
  });
  $effect(() => {
    if (draft.explanation) persisted = findingDrafts.save(key, $state.snapshot(draft));
    else {
      findingDrafts.clear(key);
      draft.expected = finding.revision;
    }
  });
  function mathEvidence(check: string, span?: string) {
    void checks.show(`${check}\u0000${span ?? ''}`);
  }
  async function respond() {
    await submit(responding, async () => {
      const result = await guardedWrite('finding', key, findingResponse(finding, draft), (sent) =>
        reviews.respond(projectId, sessionId, finding.finding_id, sent),
      );
      draft = afterResponse(draft, result);
      findingDrafts.clear(key);
      await changed();
    });
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (draft.explanation && !persisted) event.preventDefault();
  }}
/>
<article class="finding" class:resolved={folded}>
  <details open={!folded}>
    <summary
      ><h3>
        {finding.severity} · {finding.category.replaceAll('_', ' ')} ·
        <span class={`chip ${statusClass(finding.status)}`}>{statusLabel(finding)}</span>
      </h3></summary
    >
    <p>{statusText(finding.status, finding.latest_recheck)}</p>
    {#if finding.dismissal}{#each dismissalText(finding.dismissal) as line}<p>
          {visible(line)}
        </p>{/each}{/if}
    {#each successorText(finding) as line}<p>{line}</p>{/each}
    {#each recheckText(finding) as line}<p class="muted">{visible(line)}</p>{/each}
    <p class="muted">Severity {finding.severity}: {severityText(finding.severity)}.</p>
    {#if finding.origin !== 'math'}<p class="muted">{raisedText(finding, review)}</p>{/if}
    <!-- The reviewer's calls are its review's audit records, in the review panel (B10.3). -->
    {#if finding.review_id}<p class="muted">
        <a href={reviewHref(projectId, sessionId, finding.review_id)}>The review's audit records</a>
      </p>{/if}
    <p class="muted">
      <code>{finding.finding_id}</code> · revision {finding.revision} · {finding.reflags} reflags
    </p>
    {#if finding.origin === 'math'}<p>
        Mechanical math check · <code>{String(finding.source?.check ?? '')}</code>. No reviewer
        model was used.
      </p>
      {#if finding.source?.excluded}<p role="status">{String(finding.source.excluded)}</p>{/if}
    {/if}
    {#each finding.macro_context?.problems ?? [] as problem}<p role="status">
        {problem} Display uses a diagnostic context.
      </p>{/each}
    <Markdown source={finding.statement} {macros} />
    <HiddenBanner text={finding.statement} links />
    {#if copied}<CopiedNumbers stated={copied.stated} held={copied.held} />{/if}
    {#if finding.remedy}<p>Suggested remedy:</p>
      <Markdown source={finding.remedy} {macros} />
      <HiddenBanner text={finding.remedy} links />{/if}
    <h4>Where</h4>
    <ul class="anchors">
      {#each anchors as anchor, index (index)}<li>
          {#if anchor.href}<a href={anchor.href}>{anchor.text}</a>{:else}{anchor.text}{/if}
          {#if anchor.check}<button
              onclick={() => mathEvidence(anchor.check!.check, anchor.check!.math)}
              >Read mechanical evidence</button
            >{:else if anchor.evidence}<button
              onclick={() => pinned.read(anchor.evidence!.review, anchor.evidence!.evidence)}
              >Read pinned evidence</button
            >{/if}
        </li>{/each}
    </ul>
    <details>
      <summary>Anchors as recorded</summary>
      <pre>{visible(JSON.stringify(finding.anchors, null, 2))}</pre>
    </details>
    <!-- Each failure shows beside its own control, so a newer one is never hidden. -->
    {#if evidence.value}<pre aria-label="Mechanical math evidence">{evidence.value}</pre>{/if}
    {#if evidence.error}<ErrorMessage error={evidence.error} />{/if}
    {#if slices.items.length}<PinnedEvidencePreview
        text={pinned.text()}
      />{#if slices.next !== null}<button onclick={() => pinned.list.more()}
          >Read next evidence slice</button
        >{/if}{/if}
    {#if slices.error}<ErrorMessage error={slices.error} />{/if}
    <button disabled={busy || !history.available()} onclick={() => history.read()}
      >{history.started ? 'Load earlier finding history' : 'Read finding history'}</button
    >
    {#if pages.error}<ErrorMessage error={pages.error} />{/if}
    {#each history.entries() as entry (entry.revision)}<FindingTransition
        {projectId}
        {sessionId}
        {entry}
      />{/each}
    <details
      ontoggle={(event) => {
        // The latest explanation is read when the user opens the response, so it shows
        // above the decision without a click on the history.
        if (event.currentTarget.open && !history.started && !pages.loading) void history.read();
      }}
    >
      <summary>Respond as user</summary>
      <p>
        {finding.origin === 'math'
          ? 'The math checker confirms accepted or removed formulas; unavailable evidence does not resolve a finding.'
          : 'A claimed fix queues a re-check; only a re-check or the user makes a finding fixed.'} Setting
        a status, or dismissing, is your explicit decision: give the reason in the explanation.
      </p>
      <!-- What the user decides on, exactly as agents read it (threat model, A3): the
           statement, the remedy and the latest explanation an agent gave. -->
      <ApprovalText text={finding.statement} label="The finding's statement, exactly" />
      {#if finding.remedy}<ApprovalText
          text={finding.remedy}
          label="Its suggested remedy, exactly"
        />{/if}
      <LatestExplanation entries={history.entries()} loading={pages.loading} />
      <fieldset disabled={busy}>
        <p>
          Based on finding revision {draft.expected}. {#if draft.expected !== finding.revision}<button
              onclick={() => (draft.expected = finding.revision)}
              >Use current finding revision</button
            > Your draft is retained.{/if}
        </p>
        <label
          >Decision <select bind:value={draft.action}
            >{#each allowed as item (item.action)}<option value={item.action}>{item.label}</option
              >{/each}{#if !allowedNow}<option value={draft.action} disabled
                >{draft.action} (kept, not allowed now)</option
              >{/if}</select
          ></label
        >
        {#if !allowedNow}<p role="status">
            Your kept decision, {draft.action}, is not allowed while the finding is {finding.status}.
            Choose another decision to save.
          </p>{/if}
        <label
          >Explanation<textarea
            aria-label={`Finding response ${finding.finding_id}`}
            rows="4"
            maxlength="8192"
            bind:value={draft.explanation}></textarea></label
        >
        <HiddenBanner text={draft.explanation} />
        {#if draft.action === 'fix'}<label
            >Changed document revisions<input
              aria-label="Changed document revisions"
              bind:value={draft.references}
              placeholder="doc-…:2 doc-…:3"
            /></label
          >{/if}
        <button disabled={!draft.explanation.trim() || !allowedNow} onclick={respond}
          >Save user response</button
        ><button
          onclick={() => {
            draft.explanation = '';
            draft.references = '';
            findingDrafts.clear(key);
          }}>Discard response draft</button
        >
      </fieldset>
    </details>
    {#if !persisted}<p role="alert">
        Browser storage is unavailable. Keep this tab open or copy your response draft.
      </p>{/if}
    {#if responding.error}<ErrorMessage error={responding.error} />{/if}
  </details>
</article>

<style>
  .finding {
    border: 1px solid var(--border, #9996);
    border-radius: 0.4rem;
    margin: 1rem 0;
    padding: 1rem;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  pre {
    max-height: 18rem;
    overflow: auto;
  }
  .resolved {
    opacity: 0.85;
  }
  .chip {
    border: 1px solid currentColor;
    border-radius: 0.8rem;
    padding: 0 0.5rem;
    font-size: 0.85em;
    font-weight: normal;
  }
  .status-open {
    font-weight: bold;
  }
  .status-fixed,
  .status-superseded {
    opacity: 0.8;
  }
  .status-dismissed {
    font-style: italic;
  }
  .status-stale {
    border-style: dashed;
  }
  .anchors {
    padding-left: 1rem;
  }
  label {
    display: block;
    margin: 0.6rem 0;
  }
  textarea,
  input {
    width: 100%;
    box-sizing: border-box;
  }
  fieldset {
    border: 0;
    padding: 0;
  }
  button {
    margin: 0.3rem;
  }
</style>
