<!-- SPDX-License-Identifier: MIT -->
<!-- A frontier category's frontier (docs/specs/ledger.md, LG-18 to LG-23): the category,
     then its results grouped by their conditions, a page of groups at a time in the order
     of their keys, read on from the last key shown, so a change between reads moves none; the proposed candidates read in windows of the newest,
     older ones on request; the eligible results no group places; and the dominates links
     that count for nothing; then the history and the best known at a time the user picks.
     After the user's change on the page, the frontier is read again from its first group,
     and the page says what the change did. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { FrontierGroup, FrontierView } from '../lib/api-types';
  import { moreText, uniqueGroups, windowText } from '../lib/frontier';
  import { emptyList, PagedList } from '../lib/pagedList';
  import { frontierHref, ledgerHref, projectHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import Category from './FrontierCategory.svelte';
  import CategoryForm from './FrontierCategoryForm.svelte';
  import Group from './FrontierGroup.svelte';
  import History from './FrontierHistory.svelte';
  import Link from './FrontierLink.svelte';
  import Past from './FrontierPast.svelte';
  import Result from './FrontierResult.svelte';

  let { projectId, category }: { projectId: string; category: string } = $props();
  const groups = $state(emptyList<FrontierGroup, string>());
  /** The frontier as its first page of groups read it: all but its groups. */
  let read = $state<FrontierView | null>(null);
  /** The candidates read: the proposed results made before this result number, or the newest. */
  let before = $state<number | null>(null);
  /** The windows of candidates read before this one, the latest first, to go back to. */
  let newer = $state<(number | null)[]>([]);
  // The route keys this view by the project and category: they never change here.
  const list = new PagedList(groups, async (after) => {
    const found = await api.frontier(projectId, category, after, before);
    return {
      items: found.frontier.groups,
      next: found.next_groups_key,
      shown: () => {
        if (after === null) read = found.frontier;
      },
    };
  });
  void list.reload();
  const shown = $derived(uniqueGroups(groups.items));
  const measures = $derived(read?.category.measures ?? []);

  /** Read again what failed: the first page, or the next one. */
  function retry(): void {
    void (groups.items.length === 0 ? list.reload() : list.more());
  }

  /** What the user's last change did, in words. */
  let notice = $state<string | null>(null);
  /** Whether the user's revision of the category is open. */
  let revising = $state(false);
  /** Counts the user's changes on the page, so the history reads its first page again. */
  let changes = $state(0);

  /** After the user's drop of a link: say what it did, and read the frontier again. */
  async function done(text: string): Promise<void> {
    notice = text;
    changes += 1;
    await list.reload();
  }

  /** Read the frontier again with another window of candidates, from its first group. */
  function readWindow(next: number | null, back: (number | null)[]): void {
    before = next;
    newer = back;
    read = null;
    void list.restart();
  }
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
  <a href={ledgerHref(projectId)}>Ledger</a> / <a href={frontierHref(projectId)}>Frontier</a> /
</nav>
<h1>Frontier: {category}</h1>
{#if notice}<p role="status">{notice}</p>{/if}

{#if read}
  <Category category={read.category} revised={read.agent_revised} lookalike={read.lookalike} />
  {#if revising}
    <CategoryForm
      {projectId}
      current={read.category}
      done={async (saved) => {
        revising = false;
        await done(
          `Your revision r${saved.revision} of ${saved.category} is saved` +
            (saved.replayed ? ' (already: the server replayed its answer).' : '.'),
        );
      }}
      oncancel={() => (revising = false)}
    />
  {:else}
    <p><button type="button" onclick={() => (revising = true)}>Revise this category…</button></p>
  {/if}
  {#if read.truncated || before !== null}
    <p class="muted">{windowText(before, read.truncated)}</p>
  {/if}
{/if}
<!-- Outside the read, so a window that fails to read can still be left. -->
{#if (read !== null && read.next_candidates_before !== null) || before !== null}
  <p>
    {#if read !== null && read.next_candidates_before !== null}
      <button
        type="button"
        disabled={groups.loading}
        onclick={() => readWindow(read!.next_candidates_before, [before, ...newer])}
        >Older candidates</button
      >
    {/if}
    {#if before !== null}
      <button
        type="button"
        disabled={groups.loading}
        onclick={() => readWindow(newer[0] ?? null, newer.slice(1))}>Newer candidates</button
      >
    {/if}
  </p>
{/if}

{#each shown as group (group.key)}
  <Group {projectId} {group} {measures} {done} />
{:else}
  {#if read}<p class="muted">No result of this category is eligible or a candidate yet.</p>{/if}
{/each}
{#if groups.loading}<p class="muted">Loading…</p>{/if}
{#if groups.error}
  <ErrorMessage error={groups.error} />
  <p><button type="button" disabled={groups.loading} onclick={retry}>Try again</button></p>
{/if}
{#if groups.next !== null && !groups.error}
  <p>
    <button type="button" disabled={groups.loading} onclick={() => list.more()}>More groups</button>
  </p>
{/if}

{#if read && (read.unplaced.length || read.unplaced_more)}
  <h2>Unplaced</h2>
  <p class="muted">
    Eligible results without a value for each of the category's condition keys: it gained one since
    they were recorded.
  </p>
  {#each read.unplaced as result (result.entry_id)}
    <Result {projectId} {result} {measures} {done} />
  {/each}
  {#if read.unplaced_more}<p class="muted">
      {moreText({ more: { unplaced: read.unplaced_more } }, 'unplaced')}
    </p>{/if}
{/if}

{#if read && read.void_links.length}
  <h2>Links that count for nothing</h2>
  <p class="muted">
    Dominates links between eligible results that the frontier does not count, each with why. Drop
    one, or one of its cycle, to settle it.
  </p>
  <ul class="links">
    {#each read.void_links as link (`${link.entry_id}>${link.target}`)}
      <Link {projectId} {link} {done} />
    {/each}
  </ul>
{/if}

<History {projectId} {category} {changes} />
<Past {projectId} {category} />
