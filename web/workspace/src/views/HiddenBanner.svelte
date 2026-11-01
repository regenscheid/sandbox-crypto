<!-- SPDX-License-Identifier: MIT -->
<!-- What a text shown elsewhere, raw in a text box or rendered, may hide from its reader
     (threat model, A3): each character shown as its code point, with its line and column,
     and, with `links` (beside a rendering), every link address, title and reference
     definition a rendering shows only on hover or not at all, and what else it hides:
     math that draws nothing, references to hidden characters, a code fence's words after
     its language. A plain link, whose address alone shows on hover, folds into a closed
     list; when the text hides nothing else the banner is just that list. Nothing when the
     text hides nothing. With `quiet`, for a whole rendered
     document: shown only when it hides something, a link's address being in plain view
     on hover; it lists every link only for a short text, and otherwise only the links
     with a title, which the rendering hides. -->
<script lang="ts">
  import { linksIn, plainLink, renderingHides, type FoundLink } from '../lib/approval';
  import { hiddenPlaces, visible } from '../lib/text';

  let {
    text,
    links = false,
    quiet = false,
  }: { text: string; links?: boolean; quiet?: boolean } = $props();
  /** Longer than this, a quiet banner leaves out the links without a title. */
  const SHORT = 4000;
  const places = $derived(hiddenPlaces(text));
  const all = $derived(links || quiet ? linksIn(text) : []);
  const titled = $derived(all.filter((link) => link.title !== ''));
  const hides = $derived(links || quiet ? renderingHides(text) : []);
  const hiding = $derived(places.length > 0 || hides.length > 0 || titled.length > 0);
  const shown = $derived(quiet ? hiding : places.length > 0 || all.length > 0 || hides.length > 0);
  const found = $derived(quiet && text.length > SHORT ? titled : all);
  const plain = $derived(links && !quiet ? all.filter(plainLink) : []);
  const misleading = $derived(links && !quiet ? all.filter((link) => !plainLink(link)) : []);
  /** Beside a rendering, only plain links: the closed list alone, no banner. */
  const folded = $derived(
    plain.length > 0 && places.length === 0 && hides.length === 0 && misleading.length === 0,
  );
  const listed = $derived(places.slice(0, 8));
  const heading = $derived(
    `${places.length} ${places.length === 1 ? 'character' : 'characters'} you may not see ` +
      'or tell apart from others:',
  );
</script>

{#snippet rows(found: FoundLink[])}
  <ul>
    {#each found.slice(0, 20) as link, index (index)}
      <li>
        {link.kind}
        <code>{visible(link.label)}</code>:
        <code>{visible(link.address)}</code>{#if link.title}, titled
          <q>{visible(link.title)}</q>{/if}
      </li>
    {/each}
    {#if found.length > 20}<li>and {found.length - 20} more</li>{/if}
  </ul>
{/snippet}

{#snippet closed(label: string)}
  <details class="plain-links">
    <summary>{label} ({plain.length})</summary>
    {@render rows(plain)}
  </details>
{/snippet}

{#if shown && folded}
  <div class="hidden-banner folded">{@render closed('Links')}</div>
{:else if shown}
  <div role="status" class="hidden-banner">
    {#if places.length > 0}
      <p>{heading}</p>
      <ul class="places">
        {#each listed as place, index (index)}
          <li>
            <span class="chip">⟦{place.code}⟧</span> at line {place.line}, column {place.column}
          </li>
        {/each}
        {#if places.length > listed.length}<li>and {places.length - listed.length} more</li>{/if}
      </ul>
    {/if}
    {#if hides.length > 0}
      <p>What the rendering hides:</p>
      <ul>
        {#each hides.slice(0, 8) as hidden, index (index)}<li>
            {hidden.what}, at line {hidden.line}
          </li>{/each}
        {#if hides.length > 8}<li>and {hides.length - 8} more</li>{/if}
      </ul>
    {/if}
    {#if links && !quiet}
      {#if misleading.length > 0}
        <p>
          Links that may not lead where their words say, or that a rendering shows only on hover or
          not at all:
        </p>
        {@render rows(misleading)}
      {/if}
      {#if plain.length > 0}{@render closed('Other links')}{/if}
    {:else if found.length > 0}
      <p>
        {found.length < all.length
          ? 'Links with a title, which a rendering shows only on hover:'
          : 'Links, which a rendering shows only on hover or not at all:'}
      </p>
      {@render rows(found)}
    {/if}
  </div>
{/if}

<style>
  .hidden-banner {
    border-left: 3px solid currentColor;
    padding: 0.2em 0.6em;
    margin: 0.3em 0;
    overflow-wrap: anywhere;
  }
  .places li {
    display: inline;
  }
  .places li + li::before {
    content: '; ';
  }
  .places {
    padding: 0;
    margin: 0 0 0.3em;
  }
  .hidden-banner.folded {
    border-left: none;
    padding: 0;
    margin: 0.2em 0 0;
    font-size: 0.85em;
    color: var(--muted);
  }
  .plain-links summary {
    cursor: pointer;
    width: fit-content;
  }
  .plain-links ul {
    margin: 0.2em 0;
  }
  .chip {
    border: 1px solid currentColor;
    border-radius: 0.2em;
    padding: 0 0.15em;
    font-weight: bold;
  }
</style>
