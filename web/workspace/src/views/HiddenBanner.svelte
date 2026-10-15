<!-- SPDX-License-Identifier: MIT -->
<!-- What a text shown elsewhere, raw in a text box or rendered, may hide from its reader
     (threat model, A3): each character shown as its code point, with its line and column,
     and, with `links`, every link address, title and reference definition a rendering
     shows only on hover or not at all. Nothing when the text hides nothing. -->
<script lang="ts">
  import { linksIn } from '../lib/approval';
  import { hiddenPlaces, visible } from '../lib/text';

  let { text, links = false }: { text: string; links?: boolean } = $props();
  const places = $derived(hiddenPlaces(text));
  const found = $derived(links ? linksIn(text) : []);
  const listed = $derived(places.slice(0, 8));
  const heading = $derived(
    `${places.length} ${places.length === 1 ? 'character' : 'characters'} you may not see ` +
      'or tell apart from others:',
  );
</script>

{#if places.length > 0 || found.length > 0}
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
    {#if found.length > 0}
      <p>Links, which a rendering shows only on hover or not at all:</p>
      <ul>
        {#each found as link, index (index)}
          <li>
            {link.kind}
            <code>{visible(link.label)}</code>:
            <code>{visible(link.address)}</code>{#if link.title}, titled
              <q>{visible(link.title)}</q>{/if}
          </li>
        {/each}
      </ul>
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
  .chip {
    border: 1px solid currentColor;
    border-radius: 0.2em;
    padding: 0 0.15em;
    font-weight: bold;
  }
</style>
