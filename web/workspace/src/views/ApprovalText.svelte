<!-- SPDX-License-Identifier: MIT -->
<!-- A text the user approves and agents then read, shown as they will read it (threat
     model, A3): the exact string, with every character a person may not see as a chip
     naming its code point and a count of them above; with `before`, its differences from
     the text it replaces, word by word; with `preview`, a rendering apart, labelled as one,
     with every link address, title and reference definition the rendering would hide.
     Only text, never HTML, outside the Markdown component's preview. -->
<script lang="ts">
  import { linksIn, textDiff } from '../lib/approval';
  import Markdown from '../lib/Markdown.svelte';
  import { blankRun, chipped, codePoint, parts, visible } from '../lib/text';

  let {
    text,
    before = null,
    preview = false,
    label = 'The exact text',
  }: { text: string; before?: string | null; preview?: boolean; label?: string } = $props();
  const count = $derived(chipped(text));
  const banner = $derived(
    `${count} ${count === 1 ? 'character' : 'characters'} you may not see or tell apart ` +
      'from others, each shown as its code point below.',
  );
  const blanks = $derived(blankRun(text));
  const changes = $derived(before === null ? null : textDiff(before, text));
  const links = $derived(preview ? linksIn(text) : []);
</script>

<div class="approval">
  {#if count > 0}
    <p role="alert" class="banner">{banner}</p>
  {/if}
  {#if blanks !== null}
    <p role="alert" class="banner">
      More than two blank lines in a row from line {blanks}: read on to the end.
    </p>
  {/if}
  <p class="label">{label}</p>
  <pre class="exact">{#each parts(text) as part, index (index)}{#if part.hidden}<span class="chip"
          >⟦{codePoint(part.text)}⟧</span
        >{:else}{part.text}{/if}{/each}</pre>
  {#if before !== null}
    <p class="label">The changes from the text now</p>
    {#if changes === null}
      <p class="muted">The texts are too long to compare here.</p>
    {:else}
      <pre
        class="exact diff">{#each changes as change, index (index)}{#if change.kind === 'removed'}<del
              >{visible(change.text)}</del
            >{:else if change.kind === 'added'}<ins>{visible(change.text)}</ins>{:else}{visible(
              change.text,
            )}{/if}{/each}</pre>
    {/if}
  {/if}
  {#if preview}
    <details class="preview">
      <summary>A preview, as rendered: it may hide parts of the text</summary>
      <Markdown source={text} />
      {#if links.length}
        <p class="label">Links in the text, which the rendering may hide</p>
        <ul class="links">
          {#each links as link, index (index)}
            <li>
              {link.kind}
              <code>{visible(link.label)}</code>:
              <code>{visible(link.address)}</code>{#if link.title}, titled
                <q>{visible(link.title)}</q>{/if}
            </li>
          {/each}
        </ul>
      {/if}
    </details>
  {/if}
</div>

<style>
  /* The whole text, however long, comes before what the user decides with. */
  .exact {
    max-height: none;
    overflow: visible;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    margin: 0.25em 0 0.75em;
  }
  .chip {
    border: 1px solid currentColor;
    border-radius: 0.2em;
    padding: 0 0.15em;
    font-weight: bold;
  }
  .banner {
    font-weight: bold;
  }
  .label {
    margin: 0.5em 0 0;
    font-size: 0.9em;
  }
  .links {
    overflow-wrap: anywhere;
  }
</style>
