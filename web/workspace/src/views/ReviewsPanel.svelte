<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { emptyReviewPanel, panelBusy, panelError, ReviewBrowser } from '../lib/reviewBrowser';
  import FindingCard from './FindingCard.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  import PinnedEvidencePreview from './PinnedEvidencePreview.svelte';
  let { projectId, sessionId }: { projectId: string; sessionId: string } = $props();
  const panel = $state(emptyReviewPanel());
  const browser = new ReviewBrowser(panel, () => ({ project: projectId, session: sessionId }));
  const busy = $derived(panelBusy(panel));
  const error = $derived(panelError(panel));
  const selected = $derived(panel.selected);
  const refresh = () => browser.refresh();
  const read = (review: string, evidence: string) => browser.read(review, evidence);
  $effect(() => {
    void browser.refresh();
  });
</script>

<section aria-label="Consistency reviews">
  <h2>Consistency reviews</h2>
  <p class="muted">
    A pass covers the recorded checks and is not a proof. Missing evidence, capped reviews and
    unconfirmed fixes remain explicit.
  </p>
  <button disabled={busy} onclick={refresh}>Refresh reviews</button><button
    disabled={busy}
    onclick={() => browser.request()}>Request review at next live safe point</button
  >
  {#if panel.notice}<p role="status">{panel.notice}</p>{/if}
  {#if panel.unreviewed.length}<details open>
      <summary>Windows left unreviewed after bounded retries</summary>
      <pre>{JSON.stringify(panel.unreviewed, null, 2)}</pre>
    </details>{/if}
  {#if !panel.reviews.items.length}<p>No consistency reviews recorded.</p>{/if}
  <ul>
    {#each panel.reviews.items as review (review.review_id)}<li>
        <button onclick={() => browser.select(review)}
          >{review.kind} · {review.state} · {new Date(
            review.created * 1000,
          ).toLocaleString()}</button
        >
        {review.summary}
      </li>{/each}
  </ul>
  {#if panel.reviews.next}<button disabled={busy} onclick={() => browser.reviews.more()}
      >Load more reviews</button
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
      {#each panel.evidence.items as item (item.evidence)}<li>
          <button onclick={() => read(selected!.review_id, item.evidence)}
            >{item.kind} · {item.complete ? 'available' : 'incomplete'}</button
          ><code>{JSON.stringify(item.locator)}</code>
        </li>{/each}
    </ul>
    {#if panel.evidence.next !== null}<button
        disabled={busy}
        onclick={() => browser.evidence.more()}>Load more snapshot evidence</button
      >{/if}
    <button disabled={busy || panel.records.next === null} onclick={() => browser.records.more()}
      >Load review audit records</button
    >
    {#each panel.records.items as record, index (index)}<details>
        <summary>Audit record {index + 1}</summary>
        <pre>{JSON.stringify(record, null, 2)}</pre>
      </details>{/each}
  {/if}
  {#if panel.text.items.length}<PinnedEvidencePreview
      text={panel.text.items.join('')}
    />{#if panel.text.next !== null}<button onclick={() => browser.text.more()}
        >Read next evidence slice</button
      >{/if}{/if}
  <h3>Mechanical math checks</h3>
  <p class="muted">
    Mechanical checks use the operator-configured renderer. Unavailable, queued and quota-held work
    cannot establish a fix. Checks with no eligible author remain visible here.
  </p>
  {#if panel.queue.length}<details open>
      <summary>Pending or held source revisions</summary>
      <pre>{JSON.stringify(panel.queue, null, 2)}</pre>
      {#if panel.queueMore}<p>Additional sources remain queued or held.</p>{/if}
    </details>{/if}
  {#if !panel.checks.items.length}<p>No mechanical math checks recorded.</p>{/if}
  <ul>
    {#each panel.checks.items as check (check.check_id)}<li>
        <button onclick={() => browser.readMath(check.check_id)}
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
  {#if panel.checks.next}<button onclick={() => browser.checks.more()}>Load more math checks</button
    >{/if}
  {#if panel.math.items.length}<h4>Mechanical evidence and author provenance</h4>
    <pre aria-label="Document math check">{panel.math.items.join('\n')}</pre>
    {#if panel.math.next !== null}<button onclick={() => browser.math.more()}
        >Read next math evidence page</button
      >{/if}{/if}
  <h3>Findings</h3>
  {#if !panel.findings.items.length}<p>No consistency findings recorded.</p>{/if}
  {#each panel.findings.items as finding (finding.finding_id)}<FindingCard
      {projectId}
      {sessionId}
      {finding}
      changed={refresh}
      {read}
    />{/each}
  {#if panel.findings.next}<button disabled={busy} onclick={() => browser.findings.more()}
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
