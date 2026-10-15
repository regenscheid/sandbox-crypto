<!-- SPDX-License-Identifier: MIT -->
<!-- The project's frontier categories, in the order they were defined, a page at a time
     (docs/specs/ledger.md, LG-18): each leading to its frontier, with its target, kind and
     revision; and the user's definition of a new one, after which the list is read again. -->
<script lang="ts">
  import { api } from '../lib/api';
  import type { Category } from '../lib/api-types';
  import { writerText } from '../lib/frontier';
  import { emptyList, PagedList } from '../lib/pagedList';
  import { CATEGORY_NAME, categoryHref, ledgerHref, projectHref } from '../lib/router';
  import ErrorMessage from './ErrorMessage.svelte';
  import CategoryForm from './FrontierCategoryForm.svelte';

  let { projectId }: { projectId: string } = $props();
  const categories = $state(emptyList<Category, string>());
  // The route keys this view by the project: it never changes here.
  const list = new PagedList(categories, async (after) => {
    const found = await api.frontierCategories(projectId, after);
    return { items: found.page.categories, next: found.page.next_after };
  });
  void list.reload();
  let defining = $state(false);
  /** The category the user defined last, to lead to it. */
  let defined = $state<Category | null>(null);

  async function saved(category: Category): Promise<void> {
    defining = false;
    defined = category;
    await list.reload();
  }
</script>

<nav class="trail">
  <a href="#/">Projects</a> / <a href={projectHref(projectId)}>{projectId}</a> /
  <a href={ledgerHref(projectId)}>Ledger</a> /
</nav>
<h1>Frontier</h1>
<p class="muted">
  The best known results of each category: its results grouped by their conditions, and compared on
  its measures. You or the main agent define the categories.
</p>

{#if defined}
  <p role="status">
    You defined
    {#if CATEGORY_NAME.test(defined.category)}<a href={categoryHref(projectId, defined.category)}
        >{defined.category}</a
      >{:else}{defined.category}{/if}, r{defined.revision}{defined.replayed
      ? ' (already saved: the server replayed its answer)'
      : ''}.
  </p>
{/if}
{#if defining}
  <CategoryForm {projectId} done={saved} oncancel={() => (defining = false)} />
{:else}
  <p><button type="button" onclick={() => (defining = true)}>Define a category…</button></p>
{/if}

<ul>
  {#each categories.items as item (item.category)}
    <li>
      {#if CATEGORY_NAME.test(item.category)}<a href={categoryHref(projectId, item.category)}
          >{item.category}</a
        >{:else}{item.category}{/if}: {item.target}; {item.kind}
      <span class="muted">(r{item.revision} by {writerText(item.author)})</span>
    </li>
  {/each}
</ul>
{#if !categories.loading && !categories.error && categories.items.length === 0}
  <p class="muted">No frontier category is defined yet.</p>
{/if}
{#if categories.loading}<p class="muted">Loading…</p>{/if}
{#if categories.error}<ErrorMessage error={categories.error} />{/if}
{#if categories.next !== null}
  <button type="button" disabled={categories.loading} onclick={() => list.more()}>More</button>
{/if}
