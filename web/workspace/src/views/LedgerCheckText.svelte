<!-- SPDX-License-Identifier: MIT -->
<!-- What the user attests to when they record a check of an entry (threat model, A3;
     docs/specs/ledger.md, LG-17): the revision's title, statement and fields as exact text,
     every character a person may not see shown as its code point, and the statement's
     rendering apart, labelled as one. Only props, so a test renders it on the server. -->
<script lang="ts">
  import type { EntryView } from '../lib/api-types';
  import { fieldsText } from '../lib/ledgerCheck';
  import ApprovalText from './ApprovalText.svelte';

  let { entry }: { entry: EntryView } = $props();
  const fields = $derived(fieldsText(entry.fields));
</script>

<ApprovalText
  text={entry.title}
  label={`The title of ${entry.handle} r${entry.revision}, exactly`}
/>
<ApprovalText
  text={entry.statement}
  preview
  label="Its statement, exactly as agents and reviewers read it"
/>
{#if fields}
  <ApprovalText text={fields} label="Its fields, exactly" />
{:else}
  <p class="muted">It has no fields of its own.</p>
{/if}
