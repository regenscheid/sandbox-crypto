<!-- SPDX-License-Identifier: MIT -->
<!-- What the user attests to when they record a check of an entry (threat model, A3;
     docs/specs/ledger.md, LG-17): everything the revision holds that the check pins, as
     exact text, every character a person may not see shown as its code point: its title,
     for a reference the PDF it names (artifact revision and whole SHA-256), which the check
     vouches for (LG-16), its statement (with its rendering apart, labelled as one), its
     fields, every evidence link with its scope note and every `assumes` link, none of them
     cut short. Only props, so a test renders it on the server. -->
<script lang="ts">
  import type { EntryView } from '../lib/api-types';
  import { assumesText, evidenceText, fieldsText, pdfText } from '../lib/ledgerCheck';
  import ApprovalText from './ApprovalText.svelte';

  let { entry }: { entry: EntryView } = $props();
  const fields = $derived(fieldsText(entry.fields));
  const evidence = $derived(evidenceText(entry.evidence));
  const assumes = $derived(assumesText(entry.links));
  const pdf = $derived(entry.type === 'reference' ? pdfText(entry.fields) : null);
</script>

<ApprovalText
  text={entry.title}
  label={`The title of ${entry.handle} r${entry.revision}, exactly`}
/>
{#if entry.type === 'reference'}
  {#if pdf}
    <ApprovalText text={pdf} label="The PDF it names, which your check vouches for, exactly" />
  {:else}
    <p class="problem">It names no PDF: your check vouches for none.</p>
  {/if}
{/if}
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
{#if evidence}
  <ApprovalText
    text={evidence}
    label={`Its evidence, all ${entry.evidence.length} links, exactly: role, type, pinned target and scope note`}
  />
{:else}
  <p class="muted">It holds no evidence links.</p>
{/if}
{#if assumes}
  <ApprovalText text={assumes} label="The assumptions it rests on (its assumes links), exactly" />
{:else}
  <p class="muted">It assumes no assumption.</p>
{/if}
