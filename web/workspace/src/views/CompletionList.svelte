<!-- SPDX-License-Identifier: MIT -->
<!--
  The composer's completion list (B10, item 6; `src/lib/completion.ts`): a listbox of at
  most eight matches for `@` artifacts, `#` sessions or `/` skills, its highlighted option
  the box's active descendant. The keys live on the box (arrows, Enter, Escape); a pointer
  chooses without taking the focus from it. Every item shows as text.
-->
<script lang="ts">
  import {
    LABELS,
    optionText,
    type CompletionOption,
    type CompletionState,
  } from '../lib/completion';

  let {
    id,
    state,
    matches,
    choose,
  }: {
    /** The listbox's ID, which the box names in `aria-controls`. */
    id: string;
    state: CompletionState;
    matches: CompletionOption[];
    choose: (index: number) => void;
  } = $props();
</script>

{#if state.trigger}
  <div class="completion">
    <ul {id} role="listbox" aria-label={LABELS[state.trigger.kind]}>
      {#each matches as option, index (option.value)}
        {@const shown = optionText(option)}
        <li
          id={`${id}-${index}`}
          role="option"
          aria-selected={index === state.active}
          class:active={index === state.active}
          onpointerdown={(event) => {
            event.preventDefault();
            choose(index);
          }}
        >
          <span class="value">{shown.value}</span>
          {#if shown.detail}<span class="detail">{shown.detail}</span>{/if}
        </li>
      {/each}
    </ul>
    {#if state.loading}<p class="note" role="status">Reading…</p>
    {:else if state.error}<p class="note" role="alert">It could not be read: {state.error}</p>
    {:else if !matches.length}<p class="note" role="status">No matches.</p>{/if}
  </div>
{/if}

<style>
  .completion {
    position: absolute;
    left: 8px;
    bottom: 100%;
    z-index: 5;
    width: min(420px, calc(100% - 16px));
    margin-bottom: 4px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    box-shadow: 0 4px 16px #0002;
    padding: 4px;
    font-size: 13px;
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  li {
    display: flex;
    gap: 8px;
    align-items: baseline;
    padding: 4px 8px;
    border-radius: 6px;
    cursor: pointer;
  }
  li.active {
    background: var(--hover, #8882);
  }
  .value {
    overflow-wrap: anywhere;
  }
  .detail {
    margin-left: auto;
    color: var(--muted);
    font-size: 12px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 50%;
  }
  .note {
    margin: 4px 8px;
    color: var(--muted);
  }
</style>
