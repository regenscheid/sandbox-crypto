<!-- SPDX-License-Identifier: MIT -->
<!--
  Chat with the session's main agent (docs/specs/run-service.md): the messages written and
  their states, the run that holds the session with its events as they arrive, and the
  run service's state. All text is shown as text, or as Markdown for the user's and the
  agent's own words, as the run view shows them. The agent's questions to the user
  (docs/QUESTIONS.md) show above the message box while open, and below their run once
  closed; while the run that holds the session waits for an answer, a message answers its
  oldest open question, and the box says so.
-->
<script lang="ts">
  import { untrack } from 'svelte';
  import {
    ChatFeed,
    chatApi,
    forgetMessage,
    MAX_MESSAGE_BYTES,
    messageBytes,
    pendingMessages,
    sendMessage,
    startingText,
    thinking,
    type ChatView,
    type WatchedRun,
  } from '../lib/chat';
  import { chatDrafts } from '../lib/drafts';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import {
    answering,
    hintText,
    openQuestions,
    questionsApi,
    type AnswerBody,
    type Question,
  } from '../lib/questions';
  import { idle, submit } from '../lib/submit';
  import ChatQuestion from './ChatQuestion.svelte';
  import ChatRun from './ChatRun.svelte';
  import ChatWaiting from './ChatWaiting.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  import HiddenBanner from './HiddenBanner.svelte';
  import PollStatus from './PollStatus.svelte';

  let { projectId, sessionId, closed }: { projectId: string; sessionId: string; closed: boolean } =
    $props();
  const scope = untrack(() => `${projectId}:${sessionId}`);
  let draft = $state(chatDrafts.load(scope).draft ?? { text: '' });
  let persisted = $state(true);
  /** The text of a send that failed: its ID is kept for a retry until the draft changes. */
  let failed: string | null = null;
  let view = $state<PollState<ChatView>>(initialPoll());
  let feed: ChatFeed | null = null;
  const sending = $state(idle());
  const asking = $state(idle());
  const chat = $derived(view.data);
  const current = $derived(chat?.runs.at(-1));
  const earlier = $derived(chat?.runs.slice(0, -1) ?? []);
  // Messages not yet in a run's transcript: queued, held or refused.
  const waiting = $derived(chat ? pendingMessages(chat.messages, chat.runs) : []);
  const held = $derived(waiting.some((message) => message.state === 'held'));
  const starting = $derived(chat ? startingText(chat, waiting) : null);
  const bytes = $derived(messageBytes(draft.text));
  const open = $derived(chat ? openQuestions(chat.questions) : []);
  /** The question a message answers now: the oldest open one of the run holding the session. */
  const next = $derived(chat ? answering(chat.questions, chat.lease) : null);
  const waitingOn = $derived(open.filter((question) => question.run === chat?.lease?.run).length);
  // Read again with each poll, so the questions' ages move on.
  const now = $derived(chat ? Date.now() : 0);

  $effect(() => {
    const project = projectId,
      session = sessionId;
    view = initialPoll();
    const reading = new ChatFeed({
      messages: (after, signal) => chatApi.messages(project, session, after, signal),
      events: (run, after, signal) => chatApi.events(project, session, run, after, signal),
      questions: (signal) => questionsApi.list(project, session, signal),
    });
    feed = reading;
    const poller = new Poller(
      (signal) => reading.read(signal),
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
    // A changed draft gives up the failed send: the same text later is a new message.
    if (failed !== null && draft.text !== failed) {
      void forgetMessage(projectId, sessionId, failed);
      failed = null;
    }
  });

  function runState(run: WatchedRun): string {
    if (run.state === 'active') return 'running';
    return run.state === 'ended' ? 'ended' : 'interrupted, without a record';
  }
  async function send() {
    const text = draft.text;
    const sent = await submit(sending, async () => {
      await sendMessage(projectId, sessionId, text);
    });
    failed = sent ? null : text;
    if (sent) {
      // Writing again also queues held messages: read them again.
      feed?.recheck();
      if (draft.text === text) draft.text = '';
    }
  }
  async function resume() {
    if (await submit(asking, () => chatApi.resume(projectId, sessionId).then(() => undefined)))
      feed?.recheck();
  }
  function closedOf(run: WatchedRun): Question[] {
    return (chat?.questions ?? []).filter(
      (question) => question.run === run.run && question.state !== 'open',
    );
  }
  async function answer(question: Question, body: AnswerBody) {
    await questionsApi.answer(projectId, sessionId, question.question_id, body);
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
    {#each earlier as run (run.run)}
      <details>
        <summary>Run <code>{run.run}</code>: {runState(run)}</summary>
        <ChatRun {run} />
        {#each closedOf(run) as question (question.question_id)}
          <ChatQuestion {question} {now} answer={(body) => answer(question, body)} />
        {/each}
      </details>
    {/each}
    {#if current}
      <p>
        Run <code>{current.run}</code>: {runState(current)}.
        {#if chat.lease && chat.lease.run === current.run}<button
            disabled={asking.busy}
            onclick={() => cancel(current.run)}>Cancel run</button
          >{/if}
      </p>
      <ChatRun run={current} />
      {#each closedOf(current) as question (question.question_id)}
        <ChatQuestion {question} {now} answer={(body) => answer(question, body)} />
      {/each}
      {#if thinking(current)}<p role="status">The agent is thinking…</p>{/if}
    {/if}
    <ChatWaiting messages={waiting} {starting} />
    {#if held}<button disabled={asking.busy} onclick={resume}>Resume held messages</button>{/if}
    {#if asking.error}<ErrorMessage error={asking.error} />{/if}
  {/if}
  {#each open as question (question.question_id)}
    <ChatQuestion
      {question}
      {now}
      byMessage={!closed && next?.question_id === question.question_id}
      live={question.run === chat?.lease?.run}
      answer={(body) => answer(question, body)}
    />
  {/each}
  {#if !closed}
    {#if next}<p role="status" class="hint">
        Your message will answer: <q>{hintText(next.text)}</q>{#if waitingOn > 1}
          (the oldest of the {waitingOn} open questions){/if}
      </p>{/if}
    <label
      >Message to the main agent<textarea
        aria-label="Message to the main agent"
        bind:value={draft.text}
        rows="4"></textarea></label
    >
    <HiddenBanner text={draft.text} />
    {#if bytes > MAX_MESSAGE_BYTES}<p role="alert">
        The message is {bytes.toLocaleString('en-US')} bytes; a message holds at most {MAX_MESSAGE_BYTES.toLocaleString(
          'en-US',
        )}.
      </p>{/if}
    <button
      disabled={sending.busy || !draft.text.trim() || bytes > MAX_MESSAGE_BYTES}
      onclick={send}>{next ? 'Send as the answer' : 'Send'}</button
    >
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
  textarea {
    width: 100%;
    box-sizing: border-box;
  }
  label {
    display: block;
  }
  .hint {
    font-weight: bold;
  }
</style>
