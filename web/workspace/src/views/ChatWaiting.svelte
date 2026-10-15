<!-- SPDX-License-Identifier: MIT -->
<!--
  The messages no run has delivered, each with its state, and what the session is doing
  before a run can start, when the server says (`startingText`, chat.ts). Message text is
  shown as text, never as HTML. Props only, so the chat panel's polling stays in
  ChatPanel.svelte.
-->
<script lang="ts">
  import { messageState, type ChatMessage } from '../lib/chat';

  let {
    messages,
    starting = null,
  }: { messages: readonly ChatMessage[]; starting?: string | null } = $props();
</script>

{#if starting}<p role="status">{starting}</p>{/if}
{#if messages.length}
  <ul class="messages">
    {#each messages as message (message.message_id)}
      <li>
        <span class="muted">{messageState(message)}:</span>
        <span class="text">{message.text}</span>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
</style>
