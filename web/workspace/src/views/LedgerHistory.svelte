<!-- SPDX-License-Identifier: MIT -->
<!-- An entry's revisions (docs/specs/ledger.md, LG-34): read back a page at a time on
     request, each with its status, whether its statement changed, who made it, when and
     why, and where it came from when it was imported; each leads to that revision. With
     the first page, the flags cleared on the entry (LG-29): who cleared each, when and why,
     and the user's reopening of them, with a reason, each as a new flag. -->
<script lang="ts">
  import { untrack } from 'svelte';
  import { api } from '../lib/api';
  import type { ClearedFlag, EntryFlag, EntryRevision } from '../lib/api-types';
  import { Earlier } from '../lib/earlier';
  import { formatTime } from '../lib/format';
  import { authorText, provenanceText, statusText } from '../lib/ledger';
  import {
    CAUSE_WORDS,
    changeFlags,
    clearedText,
    flagSource,
    LISTED,
    MAX_REASON,
    openAgain,
    reasonProblem,
    reopenedSummary,
  } from '../lib/ledgerFlags';
  import { emptyList } from '../lib/pagedList';
  import { ledgerEntryHref } from '../lib/router';
  import { idle } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    projectId,
    entryId,
    handle,
    shown,
    changes = 0,
    changed = async () => {},
    open = [],
  }: {
    projectId: string;
    entryId: string;
    handle: string;
    shown: number;
    /** Counts the user's changes to the entry: after one, the first page is read again. */
    changes?: number;
    changed?: () => Promise<void>;
    /** The entry's open flags, by which a cleared flag open again is known. */
    open?: EntryFlag[];
  } = $props();
  const pages = $state(emptyList<EntryRevision[], number>());
  let cleared = $state<ClearedFlag[]>([]);
  // The entry view is keyed by the entry and revision it shows, so these never change here.
  const history = new Earlier(pages, async (before) => {
    const found = await api.ledgerHistory(projectId, entryId, before);
    return {
      items: [found.history.revisions],
      next: found.history.next_before,
      // Only the first page lists the cleared flags.
      shown: () => {
        if (before === null) cleared = found.history.flags_cleared;
      },
    };
  });
  // Newest first, as the server reads them back: each page newest first, and each page
  // older than the one before it.
  const revisions = $derived(pages.items.flat());

  let seen = untrack(() => changes);
  $effect(() => {
    if (changes === seen) return;
    seen = changes;
    // The entry changed: what was read of its history may be out of date.
    const started = history.started;
    history.reset();
    cleared = [];
    if (started) void history.read();
  });

  let selected = $state<Record<string, boolean>>({});
  let reason = $state('');
  let problem = $state<string | null>(null);
  const saving = $state(idle());
  /** Each cleared flag's open flag again, if it has one: it is not offered for reopening. */
  const again = $derived(
    Object.fromEntries(cleared.map((flag) => [flag.flag_id, openAgain(flag, open)])),
  );
  const chosen = $derived(
    cleared
      .filter((flag) => selected[flag.flag_id] && again[flag.flag_id] === null)
      .map((f) => f.flag_id),
  );
  let summary = $state<string | null>(null);

  async function reopen(): Promise<void> {
    problem = reasonProblem(reason);
    summary = null;
    if (problem !== null || chosen.length === 0) return;
    const change = { flags: chosen, reason };
    const answer = await changeFlags(
      saving,
      'reopen',
      projectId,
      entryId,
      change,
      (body) => api.reopenLedgerFlags(projectId, entryId, body),
      changed,
    );
    if (answer !== null) {
      summary = reopenedSummary(answer);
      reason = '';
      selected = {};
    }
  }
</script>

<h2>History</h2>
<button
  type="button"
  disabled={pages.loading || !history.available()}
  onclick={() => history.read()}
  >{history.started ? 'Load earlier revisions' : 'Read its revisions'}</button
>
{#if revisions.length}
  <ul class="history">
    {#each revisions as revision (revision.revision)}
      <li>
        {#if revision.revision === shown}<strong>Revision {revision.revision}</strong> (shown){:else}<a
            href={ledgerEntryHref(projectId, handle, revision.revision)}
            >Revision {revision.revision}</a
          >{/if}: {statusText(revision.status)}{revision.statement_changed
          ? ', statement changed'
          : ''}. {revision.title}
        <p class="muted">
          {formatTime(revision.created)} by {authorText(revision.author)}: {revision.reason}
          {#if revision.provenance}<br />{provenanceText(revision.provenance)}{/if}
        </p>
      </li>
    {/each}
  </ul>
{/if}
{#if cleared.length}
  <h3>Cleared flags</h3>
  <ul class="flags">
    {#each cleared as flag (flag.flag_id)}
      {@const source = flagSource(projectId, flag.source)}
      <li>
        {#if again[flag.flag_id] === null}
          <label>
            <input type="checkbox" bind:checked={selected[flag.flag_id]} disabled={saving.busy} />
            <strong>{CAUSE_WORDS[flag.cause]}</strong>:
          </label>
        {:else}
          <strong>{CAUSE_WORDS[flag.cause]}</strong>:
        {/if}
        {#if source.href}<a href={source.href}>{source.text}</a>{:else}{source.text}{/if}
        <span class="muted">(raised on r{flag.revision}; {flag.flag_id})</span>
        {#if again[flag.flag_id] !== null}
          <span class="muted">Open again as {again[flag.flag_id]}.</span>
        {/if}
        <p class="muted">
          {clearedText(flag, authorText(flag.cleared_by), formatTime(flag.cleared))}
        </p>
      </li>
    {/each}
  </ul>
  {#if cleared.length >= LISTED}
    <p class="muted">The latest {cleared.length} cleared flags are listed; earlier ones are not.</p>
  {/if}
  <label class="reason"
    >Reason for reopening (required)
    <textarea bind:value={reason} maxlength={MAX_REASON} rows="3" disabled={saving.busy}
    ></textarea></label
  >
  <p>
    <button type="button" disabled={saving.busy || chosen.length === 0} onclick={reopen}
      >Reopen the {chosen.length} selected</button
    >
  </p>
  {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
  {#if saving.error}<ErrorMessage error={saving.error} />{/if}
{/if}
{#if summary}<p role="status">{summary}</p>{/if}
{#if pages.error}<ErrorMessage error={pages.error} />{/if}
