<!-- SPDX-License-Identifier: MIT -->
<!-- The text a user is about to adopt, shown exactly before the user confirms (threat
     model, A3): moving a workbook document an agent wrote into the project, or restoring
     a revision, makes its text the project's. The confirmation sits below the whole text.
     Only props, so a test renders it on the server. -->
<script lang="ts">
  import ApprovalText from './ApprovalText.svelte';

  let {
    title,
    text,
    before = null,
    label,
    confirm,
    busy,
    onconfirm,
    oncancel,
  }: {
    title: string;
    text: string;
    before?: string | null;
    label: string;
    confirm: string;
    busy: boolean;
    onconfirm: () => void;
    oncancel: () => void;
  } = $props();
</script>

<section class="confirm">
  <h3>{title}</h3>
  <ApprovalText {text} {before} preview {label} />
  <button disabled={busy} onclick={onconfirm}>{confirm}</button>
  <button disabled={busy} onclick={oncancel}>Cancel</button>
</section>

<style>
  .confirm {
    padding: 0.8rem;
    border: 1px solid var(--border, #9996);
    margin: 0.5rem 0;
  }
</style>
