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
  import { attachmentDrafts, attachmentMessage } from '../lib/uploads';
  import { uploadDraft } from '../lib/uploadDrafts.svelte';
  import ChatAttachments from './ChatAttachments.svelte';
  import { chatDrafts } from '../lib/drafts';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import {
    answerChars,
    answering,
    hintText,
    MAX_ANSWER_CHARS,
    openQuestions,
    questionsApi,
    type AnswerBody,
    type Question,
  } from '../lib/questions';
  import { idle, submit } from '../lib/submit';
  import ChatQuestion from './ChatQuestion.svelte';
  import ChatRecovery from './ChatRecovery.svelte';
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
  let failed: { text: string; attachments: string[] } | null = null;
  const uploads = untrack(() => uploadDraft(projectId, sessionId));
  const attachments = $derived(uploads.files);
  const uploading = $derived(uploads.busy);
  const incomplete = $derived(uploads.pending.length > 0);
  const attached = $derived(attachments.map((file) => file.upload_id));
  let attachmentsPersisted = $state(true);
  $effect(() => {
    attachmentsPersisted = attachments.length
      ? attachmentDrafts.save(scope, $state.snapshot(attachments))
      : attachmentDrafts.clear(scope);
  });
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
  const combined = $derived(attachmentMessage(draft.text, attachments));
  const bytes = $derived(messageBytes(combined));
  const open = $derived(chat ? openQuestions(chat.questions) : []);
  /** The question a message answers now: the oldest open one of the run holding the session. */
  const next = $derived(chat ? answering(chat.questions, chat.lease) : null);
  const waitingOn = $derived(open.filter((question) => question.run === chat?.lease?.run).length);
  // Read again with each poll, so the questions' ages move on.
  const now = $derived(chat ? Date.now() : 0);
  /** A message that answers a question holds no more than an answer may. */
  const chars = $derived(next ? answerChars(combined) : 0);
  const tooLong = $derived(bytes > MAX_MESSAGE_BYTES || chars > MAX_ANSWER_CHARS);

  $effect(() => {
    const project = projectId,
      session = sessionId;
    view = initialPoll();
    const reading = new ChatFeed({
      messages: (after, signal) => chatApi.messages(project, session, after, signal),
      events: (run, after, signal) => chatApi.events(project, session, run, after, signal),
      questions: (open, signal) => questionsApi.list(project, session, open, signal),
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
    if (
      failed !== null &&
      (draft.text !== failed.text ||
        JSON.stringify(attached) !== JSON.stringify(failed.attachments))
    ) {
      void forgetMessage(projectId, sessionId, failed.text, failed.attachments);
      failed = null;
    }
  });

  function runState(run: WatchedRun): string {
    if (run.state === 'active') return 'running';
    return run.state === 'ended' ? 'ended' : 'interrupted, without a record';
  }
  async function send() {
    const text = draft.text;
    const files = [...attached];
    // The question the box said the message answers: a different one now refuses it, and
    // the draft stays for the user to read the chat again.
    const answers = next?.question_id ?? null;
    const sent = await submit(sending, async () => {
      await sendMessage(projectId, sessionId, text, undefined, answers, files);
    });
    failed = sent ? null : { text, attachments: files };
    if (sent) {
      // Writing again also queues held messages: read them again.
      feed?.recheck();
      if (draft.text === text) draft.text = '';
      uploads.files = attachments.filter((file) => !files.includes(file.upload_id));
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

{#snippet closedQuestion(question: Question)}
  <ChatQuestion {question} {now} answer={(body) => answer(question, body)} />
{/snippet}

<svelte:window
  onbeforeunload={(event) => {
    if ((draft.text && !persisted) || (attachments.length && !attachmentsPersisted))
      event.preventDefault();
  }}
/>
<section class="chat" aria-label="Chat with the main agent">
  <!-- The scroll region must be keyboard reachable without moving focus to every message. -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <div class="chat-history" role="log" aria-label="Conversation" tabindex="0">
    <PollStatus phase={view.phase} hasData={chat !== null} what="Chat views" />
    {#if chat}
      <div class="chat-connection">
        <span class="session-dot" class:closed={!chat.service}></span>{chat.service
          ? 'Connected to research agent'
          : 'Agent offline · messages will wait'}
      </div>
      {#if !current && earlier.length === 0 && waiting.length === 0}
        <div class="chat-welcome">
          <span class="welcome-symbol" aria-hidden="true">∑</span>
          <h2>Where shall we begin?</h2>
          <p>Explore a problem, investigate a paper, or develop a new research direction.</p>
          <p class="muted">
            Your plan, documents, and research agents stay alongside the conversation.
          </p>
        </div>
      {/if}
      {#each earlier as run (run.run)}
        <details>
          <summary>Run <code>{run.run}</code>: {runState(run)}</summary>
          <ChatRun {run} {projectId} questions={closedOf(run)} question={closedQuestion} />
        </details>
      {/each}
      {#if current}
        <p>
          <span class="state-badge">{runState(current)}</span>
          {#if chat.lease && chat.lease.run === current.run}<button
              disabled={asking.busy}
              onclick={() => cancel(current.run)}>Cancel run</button
            >{/if}
        </p>
        <ChatRun
          run={current}
          {projectId}
          questions={closedOf(current)}
          question={closedQuestion}
        />
        {#if thinking(current)}<p role="status">The agent is thinking…</p>{/if}
      {/if}
      <ChatWaiting messages={waiting} {starting} />
      <ChatRecovery {projectId} {sessionId} {held} busy={asking.busy} {closed} {resume} />
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
  </div>
  <div class="chat-composer">
    {#if !closed}
      <ChatAttachments {projectId} {sessionId} disabled={sending.busy} />
      {#if next}<p role="status" class="hint">
          Your message will answer: <q>{hintText(next.text)}</q>{#if waitingOn > 1}
            (the oldest of the {waitingOn} open questions){/if}
        </p>{/if}
      <label
        ><span class="sr-only">Message to the main agent</span><textarea
          aria-label="Message to the main agent"
          bind:value={draft.text}
          placeholder="Ask a question or guide the research…"
          onkeydown={(event) => {
            if (
              (event.ctrlKey || event.metaKey) &&
              event.key === 'Enter' &&
              !sending.busy &&
              !uploading &&
              !incomplete &&
              (draft.text.trim() || attachments.length) &&
              !tooLong
            ) {
              event.preventDefault();
              void send();
            }
          }}
          rows="3"></textarea></label
      >
      <HiddenBanner text={draft.text} />
      {#if bytes > MAX_MESSAGE_BYTES}<p role="alert">
          The message is {bytes.toLocaleString('en-US')} bytes; a message holds at most {MAX_MESSAGE_BYTES.toLocaleString(
            'en-US',
          )}.
        </p>{/if}
      {#if chars > MAX_ANSWER_CHARS}<p role="alert">
          As an answer, the message is {chars.toLocaleString('en-US')} characters; {'an answer holds at most'}
          {MAX_ANSWER_CHARS.toLocaleString('en-US')}.
        </p>{/if}
      <div class="composer-actions">
        <span class="muted">LaTeX supported · Ctrl / ⌘ + Enter to send</span><button
          class="primary-button"
          disabled={sending.busy ||
            uploading ||
            incomplete ||
            (!draft.text.trim() && !attachments.length) ||
            tooLong}
          onclick={send}
          >{sending.busy ? 'Sending…' : next ? 'Send as the answer' : 'Send message ↑'}</button
        >
      </div>
      {#if sending.error}<ErrorMessage error={sending.error} />{/if}
      {#if !persisted || !attachmentsPersisted}<p role="alert">
          Browser storage is unavailable. Keep this tab open or copy your message.
        </p>{/if}
    {:else}
      <p class="muted">The session is closed; it takes no messages.</p>
    {/if}
  </div>
</section>

<style>
  .chat {
    display: flex;
    flex-direction: column;
    min-height: 0;
    height: 100%;
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
