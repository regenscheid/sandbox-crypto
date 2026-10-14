<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { reviews, type Finding, type Transition } from '../lib/reviews';
  import { Detail, noDetail } from '../lib/detail';
  import { findingDrafts } from '../lib/drafts';
  import { Earlier } from '../lib/earlier';
  import { afterResponse, findingHistory, findingResponse, startResponse } from '../lib/findings';
  import { guardedWrite } from '../lib/mutation';
  import { emptyList } from '../lib/pagedList';
  import { idle, submit } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  import Markdown from '../lib/Markdown.svelte';
  import ResponseBody from './ResponseBody.svelte';
  import { macroSet } from '../lib/macros';
  let {
    projectId,
    sessionId,
    finding,
    changed,
    read,
  }: {
    projectId: string;
    sessionId: string;
    finding: Finding;
    changed: () => Promise<void>;
    read: (review: string, evidence: string) => Promise<void>;
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
  const responding = $state(idle());
  const pages = $state(emptyList<Transition[], number>());
  const history = new Earlier(
    pages,
    findingHistory((before) => reviews.finding(projectId, sessionId, finding.finding_id, before)),
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
<article class="finding">
  <h3>{finding.severity} · {finding.category} · {finding.status}</h3>
  <p class="muted">
    <code>{finding.finding_id}</code> · revision {finding.revision} · {finding.reflags} reflags
  </p>
  {#if finding.origin === 'math'}<p>
      Mechanical math check · <code>{String(finding.source?.check ?? '')}</code>. No reviewer model
      was used.
    </p>
    {#if finding.source?.excluded}<p role="status">{String(finding.source.excluded)}</p>{/if}
  {/if}
  {#each finding.macro_context?.problems ?? [] as problem}<p role="status">
      {problem} Display uses a diagnostic context.
    </p>{/each}
  <Markdown source={finding.statement} {macros} />{#if finding.remedy}<p>Suggested remedy:</p>
    <Markdown source={finding.remedy} {macros} />{/if}
  <details>
    <summary>Pinned evidence anchors</summary
    >{#each finding.anchors as anchor, index (index)}{#if anchor.check}<button
          onclick={() => mathEvidence(anchor.check!, anchor.math)}
          >Read mechanical evidence {index + 1}</button
        >{:else if anchor.review && anchor.evidence}<button
          onclick={() => read(anchor.review!, anchor.evidence!)}>Read evidence {index + 1}</button
        >{/if}
      <pre>{JSON.stringify(anchor, null, 2)}</pre>{/each}
  </details>
  <!-- Each failure shows beside its own control, so a newer one is never hidden. -->
  {#if evidence.value}<pre aria-label="Mechanical math evidence">{evidence.value}</pre>{/if}
  {#if evidence.error}<ErrorMessage error={evidence.error} />{/if}
  <button disabled={busy || !history.available()} onclick={() => history.read()}
    >{history.started ? 'Load earlier finding history' : 'Read finding history'}</button
  >
  {#if pages.error}<ErrorMessage error={pages.error} />{/if}
  {#each history.entries() as entry (entry.revision)}<div class="transition">
      <p>{entry.revision} · {entry.status} · {entry.actor}</p>
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
        : 'A claimed fix stays unconfirmed until a confirmation review.'} Confirm and dismiss below are
      explicit user decisions.
    </p>
    <fieldset disabled={busy}>
      <p>
        Based on finding revision {draft.expected}. {#if draft.expected !== finding.revision}<button
            onclick={() => (draft.expected = finding.revision)}>Use current finding revision</button
          > Your draft is retained.{/if}
      </p>
      <label
        >Decision <select bind:value={draft.action}
          ><option value="rebut">Rebut with explanation</option><option value="fix"
            >Claim fixed</option
          ><option value="confirm">Confirm fixed as user</option><option value="dismiss"
            >Dismiss as user</option
          ><option value="reopen">Reopen</option></select
        ></label
      >
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
      <button disabled={!draft.explanation.trim()} onclick={respond}>Save user response</button
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
