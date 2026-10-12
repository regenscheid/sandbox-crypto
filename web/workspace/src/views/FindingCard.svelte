<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { reviews, parseRevisionReferences, type Finding, type Transition } from '../lib/reviews';
  import { documentRequest, documentAnswered } from '../lib/documentRequests';
  import { loadDraft, saveDraft, clearDraft } from '../lib/drafts';
  import ErrorMessage from './ErrorMessage.svelte';
  import Markdown from '../lib/Markdown.svelte';
  let { projectId, sessionId, finding, changed, read }: { projectId: string; sessionId: string;
    finding: Finding; changed: () => Promise<void>; read: (review: string, evidence: string) => Promise<void> } = $props();
  const initial = untrack(() => finding);
  const key = untrack(() => `review:${sessionId}:${initial.finding_id}`);
  const draft = untrack(() => loadDraft(projectId, key));
  let explanation = $state(draft?.text ?? ''), action = $state(draft?.reason ?? 'rebut'), references = $state(draft?.retained ?? '');
  let expected = $state(draft?.base ?? initial.revision), persisted = $state(true), busy = $state(false), error = $state<unknown>(null);
  let history = $state<Transition[]>([]), before = $state<number | null>(null), historyHead = $state(initial.revision), loaded = $state(false);
  $effect(() => { if (finding.revision !== historyHead) { history = []; before = null; loaded = false; historyHead = finding.revision; } });
  $effect(() => {
    if (explanation) persisted = saveDraft(projectId, key, { base: expected, text: explanation, reason: action, retained: references, block: null });
    else { clearDraft(projectId, key); expected = finding.revision; }
  });
  async function respond() {
    busy = true; error = null;
    try {
      const body = { finding: finding.finding_id, expected, action, explanation, revisions: parseRevisionReferences(references) };
      const request = await documentRequest(projectId, key, body);
      const result = await reviews.respond(projectId, sessionId, finding.finding_id, { ...body, operation_id: request.id });
      documentAnswered(request); explanation = ''; references = ''; expected = result.revision; clearDraft(projectId, key); await changed();
    } catch (caught) { error = caught; } finally { busy = false; }
  }
  async function earlier() {
    busy = true;
    try {
      const head = finding.revision, result = await reviews.finding(projectId, sessionId, finding.finding_id, before);
      if (head !== finding.revision) return;
      history = [...(result.history ?? []), ...history]; before = result.history_before ?? null; loaded = true;
    } catch (caught) { error = caught; } finally { busy = false; }
  }
</script>
<svelte:window onbeforeunload={(event) => { if (explanation && !persisted) event.preventDefault(); }} />
<article class="finding">
  <h3>{finding.severity} · {finding.category} · {finding.status}</h3>
  <p class="muted"><code>{finding.finding_id}</code> · revision {finding.revision} · {finding.reflags} reflags</p>
  <Markdown source={finding.statement} />{#if finding.remedy}<p>Suggested remedy:</p><Markdown source={finding.remedy} />{/if}
  <details><summary>Pinned evidence anchors</summary>{#each finding.anchors as anchor, index (index)}<button onclick={() => read(anchor.review, anchor.evidence)}>Read evidence {index + 1}</button><pre>{JSON.stringify(anchor, null, 2)}</pre>{/each}</details>
  <button disabled={busy || (loaded && before === null)} onclick={earlier}>{loaded ? 'Load earlier finding history' : 'Read finding history'}</button>
  {#each history as entry (entry.revision)}<div class="transition"><p>{entry.revision} · {entry.status} · {entry.actor}</p><Markdown source={entry.reason} /><details><summary>Decision evidence and attribution</summary><pre>{JSON.stringify(entry.payload, null, 2)}</pre></details></div>{/each}
  <details><summary>Respond as user</summary>
    <p>A claimed fix stays unconfirmed until a confirmation review. Confirm and dismiss below are explicit user decisions.</p>
    <fieldset disabled={busy}>
      <p>Based on finding revision {expected}. {#if expected !== finding.revision}<button onclick={() => expected = finding.revision}>Use current finding revision</button> Your draft is retained.{/if}</p>
      <label>Decision <select bind:value={action}><option value="rebut">Rebut with explanation</option><option value="fix">Claim fixed</option><option value="confirm">Confirm fixed as user</option><option value="dismiss">Dismiss as user</option><option value="reopen">Reopen</option></select></label>
      <label>Explanation<textarea aria-label={`Finding response ${finding.finding_id}`} rows="4" maxlength="8192" bind:value={explanation}></textarea></label>
      <label>Changed document revisions<input aria-label="Changed document revisions" bind:value={references} placeholder="doc-…:2 doc-…:3" /></label>
      <button disabled={!explanation.trim()} onclick={respond}>Save user response</button><button onclick={() => { explanation = ''; references = ''; clearDraft(projectId, key); }}>Discard response draft</button>
    </fieldset>
  </details>
  {#if !persisted}<p role="alert">Browser storage is unavailable. Keep this tab open or copy your response draft.</p>{/if}
  {#if error}<ErrorMessage {error} />{/if}
</article>
<style>
.finding { border: 1px solid var(--border, #9996); border-radius: .4rem; margin: 1rem 0; padding: 1rem; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; } pre { max-height: 18rem; overflow: auto; }
.transition { border-top: 1px solid var(--border, #9996); } label { display: block; margin: .6rem 0; }
textarea, input { width: 100%; box-sizing: border-box; } fieldset { border: 0; padding: 0; } button { margin: .3rem; }
</style>
