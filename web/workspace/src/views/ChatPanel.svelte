<!-- SPDX-License-Identifier: MIT -->
<!--
  Chat with the session's main agent (docs/specs/run-service.md): the messages written and
  their states, the run that holds the session with its events as they arrive, and the
  run service's state. All text is shown as text, or as Markdown for the user's and the
  agent's own words, as the run view shows them. The conversation stays quiet: the steps
  between messages fold into one line each (conversation.ts), and the run's state shows in
  the session header (`signals`). The agent's questions to the user (docs/QUESTIONS.md)
  show above the message box while open, and where they were asked once closed; while the
  run that holds the session waits for an answer, a message answers its oldest open
  question, and the box says so.
-->
<script lang="ts">
  import { untrack } from 'svelte';
  import {
    CONTINUATION_AUTHOR,
    ChatFeed,
    chatApi,
    forgetMessage,
    MAX_MESSAGE_BYTES,
    messageBytes,
    pendingMessages,
    sendMessage,
    startingText,
    type ChatView,
    type WatchedRun,
  } from '../lib/chat';
  import { attachmentDrafts, attachmentMessage } from '../lib/uploads';
  import { uploadDraft } from '../lib/uploadDrafts.svelte';
  import ChatAttachments from './ChatAttachments.svelte';
  import Icon from './Icon.svelte';
  import ModelPicker from './ModelPicker.svelte';
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
  import type { ChatSignals } from '../lib/sessionStatus';
  import { idle, submit } from '../lib/submit';
  import ChatHold from './ChatHold.svelte';
  import ChatPermission from './ChatPermission.svelte';
  import ChatQuestion from './ChatQuestion.svelte';
  import { api, type PermissionDecision } from '../lib/api';
  import type { PendingApproval, RunTree } from '../lib/api-types';
  import { heldCall } from '../lib/conversation';
  import ChatRecovery from './ChatRecovery.svelte';
  import ChatRun from './ChatRun.svelte';
  import ChatWaiting from './ChatWaiting.svelte';
  import BrandMark from './BrandMark.svelte';
  import ErrorMessage from './ErrorMessage.svelte';
  import HiddenBanner from './HiddenBanner.svelte';
  import PollStatus from './PollStatus.svelte';
  import type { ArtifactEntry } from '../lib/api-types';
  import { readRunArtifacts, RunArtifacts, type RunListing } from '../lib/artifactStrip';
  import { emptyFindings, onFindingChange, SessionFindings } from '../lib/findingRows';
  import { reviews } from '../lib/reviews';
  import {
    closedCompletion,
    Completer,
    projectSessions,
    projectSkills,
    sessionArtifacts,
  } from '../lib/completion';
  import CompletionList from './CompletionList.svelte';

  let {
    projectId,
    sessionId,
    closed,
    signals = $bindable(null),
    subagents = null,
    showAgents = null,
    showArtifact = null,
    tree = null,
    showAgent = null,
  }: {
    projectId: string;
    sessionId: string;
    closed: boolean;
    signals?: ChatSignals | null;
    /** "2 active subagents", while any run. */
    subagents?: string | null;
    showAgents?: (() => void) | null;
    /** Open an artifact from a strip in the session's Artifacts panel (B10, item 6). */
    showArtifact?: ((entry: ArtifactEntry) => void) | null;
    /** The session's run tree, for agents' names and children's outcomes (B10). */
    tree?: RunTree | null;
    /** Opens a delegation's card in the Agents panel. */
    showAgent?: ((delegation: string) => void) | null;
  } = $props();
  let attach = $state<ReturnType<typeof ChatAttachments> | null>(null);
  let box = $state<HTMLTextAreaElement | null>(null);
  // `@` artifacts, `#` sessions and `/` skills (B10, item 6): client-side text only.
  const completion = $state(closedCompletion());
  const completer = untrack(
    () =>
      new Completer(completion, {
        artifacts: () => sessionArtifacts(projectId, sessionId),
        sessions: () => projectSessions(projectId),
        skills: () => projectSkills(projectId),
      }),
  );
  const matches = $derived(completion.trigger ? completer.matches() : []);
  const listbox = untrack(() => `completion-${projectId}-${sessionId}`);
  /** Follows the box's text and caret, opening, filtering or closing the list. */
  function follow() {
    if (box) completer.update(box.value, box.selectionStart ?? box.value.length);
  }
  function complete(index?: number) {
    if (!box) return;
    const done = completer.choose(draft.text, box.selectionStart ?? draft.text.length, index);
    if (!done) return;
    draft.text = done.text;
    const field = box;
    // The caret goes after the inserted text once the box shows it.
    queueMicrotask(() => {
      field.focus();
      field.setSelectionRange(done.caret, done.caret);
    });
  }
  /** The list's keys: arrows move, Enter chooses, Escape closes. */
  function completionKey(event: KeyboardEvent): boolean {
    if (!completion.trigger || event.ctrlKey || event.metaKey || event.altKey) return false;
    if (event.key === 'Escape') completer.dismiss();
    else if (!matches.length) return false;
    else if (event.key === 'ArrowDown') completer.move(1);
    else if (event.key === 'ArrowUp') completer.move(-1);
    else if (event.key === 'Enter' && !event.shiftKey) complete();
    else return false;
    event.preventDefault();
    event.stopPropagation();
    return true;
  }
  let attachMenu = $state(false);
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
  let feedPoller: Poller<ChatView> | null = null;
  /** The message being sent, shown at once until the chat reads it back (`id`). */
  let outgoing = $state<{ text: string; id: string | null } | null>(null);
  const sending = $state(idle());
  const asking = $state(idle());
  const chat = $derived(view.data);
  const echo = $derived(
    outgoing !== null &&
      !(outgoing.id !== null && chat?.messages.some((item) => item.message_id === outgoing?.id))
      ? outgoing.text
      : null,
  );
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
  const cannotSend = $derived(
    sending.busy ||
      uploading ||
      incomplete ||
      (!draft.text.trim() && !attachments.length) ||
      tooLong,
  );
  // Requests that wait for the user's answer (D76, D97): connector calls the proxy holds,
  // and research code's network and package requests, polled with the chat.
  let permissions = $state<PendingApproval[]>([]);
  $effect(() => {
    const project = projectId,
      session = sessionId;
    return new Poller(
      (signal) => api.permissionRequests(project, session, signal),
      (value) => {
        // A workspace without approvals answers no list: none wait.
        const requests = value.data?.requests;
        if (Array.isArray(requests)) permissions = requests.filter((item) => !item.timed_out);
      },
      { intervalMs: 2000 },
    ).watch();
  });
  const transcript = $derived(chat?.runs.flatMap((run) => run.events) ?? []);
  /** The session's automatic continuations, which the conversation shows folded. */
  const continuations = $derived(
    new Set(
      (chat?.messages ?? [])
        .filter((message) => message.author === CONTINUATION_AUTHOR)
        .map((message) => message.message_id),
    ),
  );
  async function answerPermission(request: PendingApproval, decision: PermissionDecision) {
    await api.answerPermission(projectId, request, decision, sessionId);
    permissions = permissions.filter(
      (item) =>
        !(item.seq === request.seq && item.kind === request.kind && item.source === request.source),
    );
  }
  $effect(() => {
    signals = chat
      ? {
          service: chat.service,
          lease: chat.lease?.run ?? null,
          questions: waitingOn + permissions.length,
          held,
        }
      : null;
  });
  // The reviewer's findings the conversation shows (B10, item 4): each read when first
  // shown, again at the chat's cadence while a run holds the session, and again whenever
  // the review panel changes one.
  const findingState = $state(emptyFindings());
  let findingPoller: Poller<void> | null = null;
  const findings = untrack(
    () =>
      new SessionFindings(
        findingState,
        (finding) => reviews.finding(projectId, sessionId, finding),
        () => findingPoller?.poke(),
      ),
  );
  $effect(() => {
    const project = projectId,
      session = sessionId;
    const poller = (findingPoller = new Poller(
      (signal) =>
        findings.refresh(
          untrack(() => (chat?.lease ?? null) !== null),
          signal,
        ),
      () => {},
      { intervalMs: 1500 },
    ));
    const off = onFindingChange(project, session, () => findings.invalidate());
    const stop = poller.watch();
    return () => {
      off();
      stop();
    };
  });
  // The runs' artifact lists, for the strips of files under the work groups (B10, item 6).
  const artifactState = $state<Record<string, RunListing>>({});
  const artifacts = untrack(
    () => new RunArtifacts(artifactState, (run) => readRunArtifacts(projectId, sessionId, run)),
  );
  /** Whether a run is working now, rather than waiting for an answer. */
  const live = (run: WatchedRun) =>
    run.state === 'active' && chat?.lease?.run === run.run && waitingOn === 0;

  $effect(() => {
    const project = projectId,
      session = sessionId;
    view = initialPoll();
    const reading = new ChatFeed({
      messages: (after, signal) => chatApi.messages(project, session, after, signal),
      events: (run, after, signal) => chatApi.events(project, session, run, after, signal),
      questions: (open, signal) => questionsApi.list(project, session, open, signal),
      runs: (signal) => api.session(project, session, signal).then((detail) => detail.runs),
    });
    feed = reading;
    const poller = (feedPoller = new Poller(
      (signal) => reading.read(signal),
      (value) => {
        view = value;
      },
      { intervalMs: 1500 },
    ));
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

  async function send() {
    const text = draft.text;
    const files = [...attached];
    // The question the box said the message answers: a different one now refuses it, and
    // the draft stays for the user to read the chat again.
    const answers = next?.question_id ?? null;
    outgoing = { text, id: null };
    const sent = await submit(sending, async () => {
      const answer = await sendMessage(projectId, sessionId, text, undefined, answers, files);
      // An answer to a question shows with the question, not as a message.
      outgoing = answer.message ? { text, id: answer.message.message_id } : null;
    });
    failed = sent ? null : { text, attachments: files };
    if (!sent) outgoing = null;
    if (sent) {
      // Writing again also queues held messages: read them again, now.
      feed?.recheck();
      feedPoller?.poke();
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
</script>

{#snippet closedQuestion(question: Question)}
  <ChatQuestion {question} {now} answer={(body) => answer(question, body)} />
{/snippet}

<svelte:window
  onpointerdown={(event) => {
    if (attachMenu && !(event.target as Element).closest?.('.composer-bar')) attachMenu = false;
  }}
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
      {#if chat.runs.length === 0 && waiting.length === 0}
        <div class="chat-welcome">
          <BrandMark size={48} />
          <h2>Where shall we begin?</h2>
          <p>Explore a problem, investigate a paper, or develop a new research direction.</p>
          <p class="muted">
            Your plan, documents, and research agents stay alongside the conversation.
          </p>
        </div>
      {/if}
      {#each chat.runs as run (run.run)}
        <ChatRun
          {run}
          {projectId}
          questions={closedOf(run)}
          question={closedQuestion}
          live={live(run)}
          {now}
          {subagents}
          {showAgents}
          {continuations}
          {sessionId}
          {findings}
          {artifacts}
          {showArtifact}
          {tree}
          {showAgent}
        />
      {/each}
      <ChatWaiting messages={waiting} {starting} sending={echo} />
      <ChatRecovery {projectId} {sessionId} {held} busy={asking.busy} {closed} {resume} />
      {#if asking.error}<ErrorMessage error={asking.error} />{/if}
    {/if}
    {#each permissions as request (`${request.source}:${request.kind}:${request.seq}`)}
      {#if request.kind === 'hold'}
        <ChatHold {request} answer={(decision) => answerPermission(request, decision)} />
      {:else}
        <ChatPermission
          {request}
          call={request.kind === 'mcp' ? heldCall(transcript, request.tool) : null}
          answer={(decision) => answerPermission(request, decision)}
        />
      {/if}
    {/each}
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
      {#if next}<p role="status" class="hint">
          Your message will answer: <q>{hintText(next.text)}</q>{#if waitingOn > 1}
            (the oldest of the {waitingOn} open questions){/if}
        </p>{/if}
      <div class="composer-box">
        <ChatAttachments
          bind:this={attach}
          {projectId}
          {sessionId}
          disabled={sending.busy}
          actions={false}
        />
        <CompletionList
          id={listbox}
          state={completion}
          {matches}
          choose={(index) => complete(index)}
        />
        <label
          ><span class="sr-only">Message to the main agent</span><textarea
            aria-label="Message to the main agent"
            aria-autocomplete="list"
            aria-controls={completion.trigger ? listbox : undefined}
            aria-activedescendant={completion.trigger && matches.length
              ? `${listbox}-${completion.active}`
              : undefined}
            bind:this={box}
            bind:value={draft.text}
            placeholder={next
              ? 'Answer in your own words…'
              : 'Ask a question or guide the research… (@ artifact, # session, / skill)'}
            oninput={follow}
            onclick={follow}
            onblur={() => completer.close()}
            onkeyup={(event) => {
              if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') follow();
            }}
            onkeydown={(event) => {
              if (completionKey(event)) return;
              if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !cannotSend) {
                event.preventDefault();
                void send();
              }
            }}
            rows="2"></textarea></label
        >
        <div class="composer-bar">
          <div class="model-picker">
            <button
              type="button"
              class="round-button"
              aria-label="Attach files"
              aria-haspopup="menu"
              aria-expanded={attachMenu}
              disabled={sending.busy}
              onclick={() => (attachMenu = !attachMenu)}><Icon name="plus" size={18} /></button
            >
            {#if attachMenu}<div class="model-popover attach-menu" role="menu">
                <button
                  type="button"
                  role="menuitem"
                  class="model-option"
                  onclick={() => {
                    attachMenu = false;
                    attach?.pick();
                  }}>Upload files…</button
                ><button
                  type="button"
                  role="menuitem"
                  class="model-option"
                  onclick={() => {
                    attachMenu = false;
                    attach?.stored();
                  }}>Attach a stored file</button
                >
                <div class="composer-note">Or drop files on the box · up to 100 MiB each</div>
              </div>{/if}
          </div>
          <span class="spacer"></span>
          <ModelPicker {projectId} />
          <button
            type="button"
            class="send-button"
            disabled={cannotSend}
            title="Send (Ctrl / ⌘ + Enter)"
            onclick={send}
            >{#if sending.busy}Sending…{:else if next}Send as the answer{:else}<Icon
                name="send"
                size={16}
              /><span class="sr-only">Send message</span>{/if}</button
          >
        </div>
      </div>
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
      <div class="composer-note">
        The agent reads your message at its next step · LaTeX supported · @ artifact, # session, /
        skill · Ctrl / ⌘ + Enter to send
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
  label {
    display: block;
  }
  .attach-menu {
    left: 0;
    right: auto;
    width: 240px;
    gap: 4px;
  }
</style>
