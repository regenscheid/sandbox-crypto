<!-- SPDX-License-Identifier: MIT -->
<!--
  The messages no run has delivered, each with its state, and what the session is doing
  before a run can start, when the server says (`startingText`, chat.ts). Message text is
  shown as text, never as HTML. `sending` is the user's message on its way, shown at once
  rather than at the chat's next read. Props only, so the chat panel's polling stays in
  ChatPanel.svelte.
-->
<script lang="ts">
  import { CONTINUATION_AUTHOR, messageState, type ChatMessage } from '../lib/chat';
  import { splitAttachments } from '../lib/uploads';
  import FileChips from './FileChips.svelte';

  let {
    messages,
    starting = null,
    sending = null,
  }: {
    messages: readonly ChatMessage[];
    starting?: string | null;
    sending?: string | null;
  } = $props();
</script>

{#if starting}<p role="status" class="transcript-note">{starting}</p>{/if}
{#if messages.length || sending !== null}
  <ul class="waiting-messages">
    {#each messages as message (message.message_id)}
      {@const split = splitAttachments(message.text)}
      {#if message.author === CONTINUATION_AUTHOR}
        <li class="continuation">
          <details>
            <summary>{messageState(message)} · Continuation</summary>
            <span class="text">{split.text}</span>
          </details>
        </li>
      {:else}
        <li class="bubble">
          <span class="meta">{messageState(message)}</span>
          <span class="text">{split.text}</span>
          <FileChips files={split.files} />
        </li>
      {/if}
    {/each}
    {#if sending !== null}
      {@const split = splitAttachments(sending)}
      <li class="bubble sending">
        <span class="meta">Sending…</span>
        <span class="text">{split.text}</span>
      </li>
    {/if}
  </ul>
{/if}

<style>
  .text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .bubble {
    max-width: 80%;
    background: var(--subtle);
    border-radius: 16px;
    padding: 10px 14px;
  }
  .sending {
    opacity: 0.7;
  }
  .meta {
    display: block;
    font-size: 12px;
    color: var(--muted);
  }
  .continuation {
    align-self: flex-end;
    max-width: 80%;
    font-size: 13px;
    color: var(--muted);
    text-align: right;
  }
  .continuation summary {
    cursor: pointer;
  }
  .continuation .text {
    display: block;
    margin-top: 6px;
    text-align: left;
    color: var(--body-text);
  }
</style>
