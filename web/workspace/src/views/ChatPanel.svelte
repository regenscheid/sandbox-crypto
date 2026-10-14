<!-- SPDX-License-Identifier: MIT -->
<!--
  Chat with the session's main agent (docs/specs/run-service.md): the messages written and
  their states, the run that holds the session with its events as they arrive, and the
  run service's state. All text is shown as text, or as Markdown for the user's and the
  agent's own words, as the run view shows them.
-->
<script lang="ts">
  import { untrack } from 'svelte';
  import {
    ChatFeed,
    chatApi,
    chatEntries,
    messageState,
    pendingMessages,
    sendMessage,
    thinking,
    type ChatView,
  } from '../lib/chat';
  import { chatDrafts } from '../lib/drafts';
  import Markdown from '../lib/Markdown.svelte';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { idle, submit } from '../lib/submit';
  import ErrorMessage from './ErrorMessage.svelte';
  import PollStatus from './PollStatus.svelte';

  let { projectId, sessionId, closed }: { projectId: string; sessionId: string; closed: boolean } =
    $props();
  const scope = untrack(() => `${projectId}:${sessionId}`);
  let draft = $state(chatDrafts.load(scope).draft ?? { text: '' });
  let persisted = $state(true);
  let view = $state<PollState<ChatView>>(initialPoll());
  const sending = $state(idle());
  const asking = $state(idle());
  const chat = $derived(view.data);
  const entries = $derived(chatEntries(chat?.run?.events ?? []));
  // Messages not yet in a run's transcript: queued, held or refused.
  const waiting = $derived(chat ? pendingMessages(chat.messages, chat.run) : []);
  const held = $derived(waiting.some((message) => message.state === 'held'));

  $effect(() => {
    const project = projectId,
      session = sessionId;
    view = initialPoll();
    const feed = new ChatFeed({
      messages: (after, signal) => chatApi.messages(project, session, after, signal),
      events: (run, after, signal) => chatApi.events(project, session, run, after, signal),
    });
    const poller = new Poller(
      (signal) => feed.read(signal),
      (value) => {
        view = value;
      },
      { intervalMs: 1500 },
    );
    return poller.watch();
  });
  $effect(() => {
    if (draft.text) persisted = chatDrafts.save(scope, $state.snapshot(draft));
    else chatDrafts.clear(scope);
  });

  async function send() {
    await submit(sending, async () => {
      await sendMessage(projectId, sessionId, draft.text);
      draft.text = '';
    });
  }
  async function resume() {
    await submit(asking, async () => {
      await chatApi.resume(projectId, sessionId);
    });
  }
  async function cancel(run: string) {
    await submit(asking, async () => {
      await chatApi.cancel(projectId, sessionId, run);
    });
  }
</script>

<svelte:window
  onbeforeunload={(event) => {
    if (draft.text && !persisted) event.preventDefault();
  }}
/>
<section class="chat" aria-label="Chat with the main agent">
  <h2>Chat</h2>
  <PollStatus phase={view.phase} hasData={chat !== null} what="Chat views" />
  {#if chat}
    <p class="muted">
      {#if chat.service}The run service is running.{:else}The run service is not running: messages
        wait until it starts.{/if}
    </p>
    {#if chat.run}
      <p>
        Run <code>{chat.run.run}</code>:
        {chat.run.state === 'active'
          ? 'running'
          : chat.run.state === 'ended'
            ? 'ended'
            : 'interrupted, without a record'}.
        {#if chat.lease && chat.lease.run === chat.run.run}<button
            disabled={asking.busy}
            onclick={() => cancel(chat.run!.run)}>Cancel run</button
          >{/if}
      </p>
      <ol class="transcript">
        {#each entries as entry (entry.seq)}
          <li>
            {#if entry.kind === 'user'}
              <h3>You</h3>
              <Markdown source={entry.text} />
            {:else if entry.kind === 'assistant'}
              <h3>Agent</h3>
              {#if entry.text}<Markdown source={entry.text} />{/if}
              {#each entry.calls as call, index (index)}
                <details>
                  <summary>Calls <code>{call.name}</code></summary>
                  <pre>{call.arguments}</pre>
                </details>
              {/each}
            {:else if entry.kind === 'tool'}
              <details>
                <summary><code>{entry.name}</code>: {entry.outcome}</summary>
                <pre>{entry.result}</pre>
              </details>
            {:else if entry.kind === 'note'}
              <p class:problem={entry.problem} class="muted">{entry.text}</p>
            {:else}
              <p class="muted">{entry.text}</p>
            {/if}
          </li>
        {/each}
      </ol>
      {#if thinking(chat.run)}<p role="status">The agent is thinking…</p>{/if}
    {/if}
    {#if waiting.length}
      <ul class="messages">
        {#each waiting as message (message.message_id)}
          <li>
            <span class="muted">{messageState(message)}:</span>
            <span class="text">{message.text}</span>
          </li>
        {/each}
      </ul>
    {/if}
    {#if held}<button disabled={asking.busy} onclick={resume}>Resume held messages</button>{/if}
    {#if asking.error}<ErrorMessage error={asking.error} />{/if}
  {/if}
  {#if !closed}
    <label
      >Message to the main agent<textarea
        aria-label="Message to the main agent"
        bind:value={draft.text}
        rows="4"
        maxlength="65536"></textarea></label
    >
    <button disabled={sending.busy || !draft.text.trim()} onclick={send}>Send</button>
    {#if sending.error}<ErrorMessage error={sending.error} />{/if}
    {#if !persisted}<p role="alert">
        Browser storage is unavailable. Keep this tab open or copy your message.
      </p>{/if}
  {:else}
    <p class="muted">The session is closed; it takes no messages.</p>
  {/if}
</section>

<style>
  .chat {
    border-top: 1px solid var(--border, #9996);
    margin-top: 1rem;
  }
  .transcript {
    list-style: none;
    padding: 0;
  }
  .transcript li {
    margin: 0.6rem 0;
  }
  .transcript h3 {
    font-size: 1rem;
    margin: 0;
  }
  .text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 20rem;
    overflow: auto;
  }
  textarea {
    width: 100%;
    box-sizing: border-box;
  }
  label {
    display: block;
  }
  .problem {
    color: var(--problem, #b3261e);
  }
</style>
