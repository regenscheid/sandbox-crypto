<!-- SPDX-License-Identifier: MIT -->
<!--
  The messages no run has delivered, each with its state, and what the session is doing
  before a run can start, when the server says (`startingText`, chat.ts). Message text is
  shown as text, never as HTML. Props only, so the chat panel's polling stays in
  ChatPanel.svelte.
-->
<script lang="ts">
  import { messageState, type ChatMessage } from '../lib/chat';
  import { splitAttachments } from '../lib/uploads';
  import FileChips from './FileChips.svelte';

  let {
    messages,
    starting = null,
  }: { messages: readonly ChatMessage[]; starting?: string | null } = $props();
</script>

{#if starting}<p role="status" class="transcript-note">{starting}</p>{/if}
{#if messages.length}
  <ul class="waiting-messages">
    {#each messages as message (message.message_id)}
      {@const split = splitAttachments(message.text)}
      <li class="bubble">
        <span class="meta">{messageState(message)}</span>
        <span class="text">{split.text}</span>
        <FileChips files={split.files} />
      </li>
    {/each}
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
  .meta {
    display: block;
    font-size: 12px;
    color: var(--muted);
  }
</style>
