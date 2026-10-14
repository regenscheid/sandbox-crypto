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
    raisedText,
    severityText,
    statusText,
    transitionText,
  } from '../lib/findingView';
  import { guardedWrite } from '../lib/mutation';
  import { emptyList } from '../lib/pagedList';
  import { idle, submit } from '../lib/submit';
  import { EvidenceText } from '../lib/evidenceText';
  import ErrorMessage from './ErrorMessage.svelte';
  import PinnedEvidencePreview from './PinnedEvidencePreview.svelte';
  import Markdown from '../lib/Markdown.svelte';
  import ResponseBody from './ResponseBody.svelte';
  import { macroSet } from '../lib/macros';
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
        {finding.severity} · {finding.category.replaceAll('_', ' ')} · {finding.status}
      </h3></summary
    >
    <p>{statusText(finding.status)}</p>
    <p class="muted">Severity {finding.severity}: {severityText(finding.severity)}.</p>
    {#if finding.origin !== 'math'}<p class="muted">{raisedText(finding, review)}</p>{/if}
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
    <Markdown source={finding.statement} {macros} />{#if finding.remedy}<p>Suggested remedy:</p>
      <Markdown source={finding.remedy} {macros} />{/if}
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
      <pre>{JSON.stringify(finding.anchors, null, 2)}</pre>
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
    {#each history.entries() as entry (entry.revision)}<div class="transition">
        <p>{transitionText(entry)}</p>
        {#each entry.macro_context?.problems ?? [] as problem}<p role="status">
            {problem}
          </p>{/each}<Markdown
          source={entry.reason}
          macros={macroSet(
            entry.macro_context?.macros ?? {},
            `${projectId}:${entry.macro_context?.key ?? 'unknown-history'}`,
          )}
        />
        <details>
          <summary>Decision evidence and attribution</summary>
          <pre>{JSON.stringify(entry.payload, null, 2)}</pre>
        </details>
        {#if entry.response_body}<ResponseBody
            {projectId}
            {sessionId}
            source={entry.response_body}
          />{/if}
      </div>{/each}
    <details>
      <summary>Respond as user</summary>
      <p>
        {finding.origin === 'math'
          ? 'The math checker confirms accepted or removed formulas; unavailable evidence does not resolve a finding.'
          : 'A claimed fix stays unconfirmed until a confirmation review.'} Confirm and dismiss below
        are explicit user decisions.
      </p>
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
        <label
          >Changed document revisions<input
            aria-label="Changed document revisions"
            bind:value={draft.references}
            placeholder="doc-…:2 doc-…:3"
          /></label
        >
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
  .transition {
    border-top: 1px solid var(--border, #9996);
  }
  .resolved {
    opacity: 0.85;
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
