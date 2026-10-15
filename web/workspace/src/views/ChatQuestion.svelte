<!-- SPDX-License-Identifier: MIT -->
<!-- A question the main agent asks the user (docs/QUESTIONS.md, PD-23 to PD-26). Open, it
     shows its age, the agent's text exactly (ApprovalText) and its options with every
     character a person may not see shown as its code point, and takes the answer: an
     option, several, the agent to decide, or a cancel; the user's own words go in the
     message box. An open question whose run no longer holds the session takes no answer:
     the server refuses one (`run_ended`), since no one would read it. Closed, it says how,
     and why when no answer came. A question is not a permission request: an answer grants
     nothing. -->
<script lang="ts">
  import type { Question } from '../lib/api-types';
  import { ageText, closedText, type AnswerBody } from '../lib/questions';
  import { idle, submit } from '../lib/submit';
  import { visible } from '../lib/text';
  import ApprovalText from './ApprovalText.svelte';
  import ErrorMessage from './ErrorMessage.svelte';

  let {
    question,
    now,
    byMessage = false,
    live = true,
    answer,
  }: {
    question: Question;
    /** The time to count the question's age from, in milliseconds. */
    now: number;
    /** A message the user writes now answers this question. */
    byMessage?: boolean;
    /** The run that asked holds the session, so an answer reaches it. */
    live?: boolean;
    answer: (body: AnswerBody) => Promise<void>;
  } = $props();
  /** The options ticked, by their place: a label such as `__proto__` is no key. */
  let chosen = $state<boolean[]>([]);
  /** An answer was sent: the question closes at the next read. */
  let sent = $state(false);
  const sending = $state(idle());
  const open = $derived(question.state === 'open');
  const picked = $derived(
    question.options.filter((_, index) => chosen[index] === true).map((option) => option.label),
  );

  async function send(body: AnswerBody): Promise<void> {
    if (await submit(sending, () => answer(body))) {
      chosen = [];
      sent = true;
    }
  }
</script>

<section class="question" class:open aria-label="A question from the agent">
  {#if open}
    <h3>The agent asks you <span class="muted">(asked {ageText(question.asked_at, now)})</span></h3>
  {:else}
    <h3>The agent asked you</h3>
  {/if}
  <ApprovalText text={question.text} label="The agent's question" preview />
  {#if open && !live}
    <p class="muted">
      Its run no longer holds the session, so it takes no answer; it closes when the run service
      ends that run. Write to the agent as a message instead.
    </p>
  {:else if open && sent}
    <p role="status">Your answer is sent; the agent reads it in a moment.</p>
  {:else if open}
    {#if question.options.length > 0}
      <ul class="options">
        {#each question.options as option, index (index)}
          <li>
            {#if question.multiple}
              <label>
                <input type="checkbox" bind:checked={chosen[index]} disabled={sending.busy} />
                {visible(option.label)}
              </label>
            {:else}
              <button
                type="button"
                disabled={sending.busy}
                onclick={() => send({ kind: 'picked', picked: [option.label] })}
                >{visible(option.label)}</button
              >
            {/if}
            {#if option.detail}<span class="muted">{visible(option.detail)}</span>{/if}
          </li>
        {/each}
      </ul>
      {#if question.multiple}
        <button
          type="button"
          disabled={sending.busy || picked.length === 0}
          onclick={() => send({ kind: 'picked', picked })}
          >Answer with the {picked.length} chosen</button
        >
      {/if}
    {/if}
    <button type="button" disabled={sending.busy} onclick={() => send({ kind: 'decide' })}
      >Let the agent decide</button
    >
    <button type="button" disabled={sending.busy} onclick={() => send({ kind: 'cancel' })}
      >Cancel the question</button
    >
    {#if byMessage}
      <p class="muted">Or answer in your own words: write it in the message box.</p>
    {/if}
    {#if sending.error}<ErrorMessage error={sending.error} />{/if}
  {:else}
    <p>{closedText(question)}</p>
    {#if question.answer?.kind === 'free' && question.answer.text}
      <pre class="answer">{visible(question.answer.text)}</pre>
    {/if}
  {/if}
</section>

<style>
  .question {
    border-left: 3px solid var(--border, #9996);
    padding-left: 0.6rem;
    margin: 0.6rem 0;
  }
  .question.open {
    border-left-color: var(--accent, #1a73e8);
  }
  h3 {
    font-size: 1rem;
    margin: 0;
  }
  .options {
    list-style: none;
    padding: 0;
  }
  .options li {
    margin: 0.3rem 0;
  }
  .answer {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
</style>
