<!-- SPDX-License-Identifier: MIT -->
<!-- A frontier category as its frontier reads it (docs/specs/ledger.md, LG-18, LG-19,
     LG-21): first the main agent's revision of it that the user has not revised since,
     with what it changed; then its target, kind, measures with their units and
     directions, condition keys, and who wrote its revision, when and why; and the groups
     whose conditions differ only in characters that look alike, as a warning. All of it
     text. Only props, so a test renders it on the server. -->
<script lang="ts">
  import type { Category, CategoryRevised } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { visible } from '../lib/text';
  import {
    lookalikeText,
    measureText,
    revisedSince,
    revisedText,
    writerText,
  } from '../lib/frontier';

  let {
    category,
    revised,
    lookalike,
  }: {
    category: Category;
    revised: CategoryRevised | null;
    lookalike: Record<string, string>[][];
  } = $props();
</script>

{#if revised}
  <div role="note" class="frontier-warning">
    <p><strong>{revisedText(revised)}</strong></p>
    <p>Its reason: {revised.reason}</p>
    <p>{revisedSince(revised)}</p>
    {#if revised.since !== null && revised.changed.length}
      <ul>
        {#each revised.changed as change, index (index)}<li>{change}</li>{/each}
      </ul>
    {/if}
  </div>
{/if}

<dl class="facts">
  <dt>Target</dt>
  <dd>{category.target}</dd>
  <dt>Kind of result</dt>
  <dd>{category.kind}</dd>
  <dt>Measures</dt>
  <dd>
    <ul class="links">
      {#each category.measures as measure (measure.name)}<li>{measureText(measure)}</li>{/each}
    </ul>
  </dd>
  <dt>Condition keys</dt>
  <dd>
    {category.conditions.length
      ? category.conditions.map(visible).join(', ')
      : 'None: every result is compared with every other'}
  </dd>
  <dt>Revision</dt>
  <dd>
    r{category.revision}, {formatTime(category.created)} by {writerText(category.author)}:
    {category.reason}
  </dd>
</dl>

{#if lookalike.length}
  <div role="alert" class="frontier-warning">
    <p>
      <strong
        >These groups' conditions differ only in characters that look alike, so they are compared
        apart. Each character outside plain ASCII is named by its code point:</strong
      >
    </p>
    <ul>
      {#each lookalike as set, index (index)}
        <li>
          {#each set as conditions, inner (inner)}{inner > 0 ? ' / ' : ''}{lookalikeText(
              conditions,
            )}{/each}
        </li>
      {/each}
    </ul>
  </div>
{/if}
