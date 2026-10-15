<!-- SPDX-License-Identifier: MIT -->
<!-- A plan's text as the user is about to save it (threat model, A3): saving makes every
     field the user's, agents' wording included, so each field is shown exactly as agents
     will read it, each hidden character a code point, with its changes from the version
     it is based on. The save sits below all of it. Only props, so a test renders it on the
     server. -->
<script lang="ts">
  import ApprovalText from './ApprovalText.svelte';

  let {
    fields,
    author,
    busy,
    onconfirm,
    oncancel,
  }: {
    fields: { key: string; label: string; value: string; before: string | null }[];
    /** Who wrote the version the draft is based on. */
    author: string;
    busy: boolean;
    onconfirm: () => void;
    oncancel: () => void;
  } = $props();
</script>

<section class="plan-review">
  <h3>Save this plan text as yours</h3>
  <p>
    The version it is based on is by {author}. Saving makes all of its text yours, as written below;
    every agent of the session reads it.
  </p>
  {#each fields as field (field.key)}
    <ApprovalText
      text={field.value}
      before={field.before !== null && field.before !== field.value ? field.before : null}
      label={field.label}
    />
  {/each}
  <button type="button" disabled={busy} onclick={onconfirm}>Save plan text</button>
  <button type="button" disabled={busy} onclick={oncancel}>Back to the draft</button>
</section>

<style>
  .plan-review {
    padding: 0.8rem;
    border: 1px solid var(--border, #9996);
    margin: 0.5rem 0;
  }
</style>
