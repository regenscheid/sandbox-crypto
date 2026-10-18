<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api } from '../lib/api';
  import type { CapturedCall, ContextPart, ContextText } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import ErrorMessage from './ErrorMessage.svelte';

  let { project, session, run }: { project: string; session: string; run: string } = $props();
  let calls = $state<CapturedCall[]>([]);
  let selected = $state<CapturedCall | null>(null);
  let parts = $state<ContextPart[]>([]);
  let current = $state<ContextPart | null>(null);
  let text = $state<ContextText | null>(null);
  let before = $state<number | null>(null);
  let after = $state<number | null>(null);
  let scope = $state<'attempt' | 'agent'>('attempt');
  let coverage = $state('');
  let busy = $state(false);
  let loaded = $state(false);
  let error = $state<unknown>(null);
  let controller: AbortController | null = null;
  let generation = 0;

  async function perform(action: (signal: AbortSignal, current: () => boolean) => Promise<void>) {
    controller?.abort();
    controller = new AbortController();
    const ticket = ++generation;
    busy = true;
    error = null;
    try {
      await action(controller.signal, () => ticket === generation);
    } catch (caught) {
      if (ticket === generation) error = caught;
    } finally {
      if (ticket === generation) busy = false;
    }
  }
  function refresh(older = false) {
    return perform(async (signal, valid) => {
      const page = await api.contextCaptures(
        project,
        session,
        run,
        scope,
        older ? (before ?? 0) : 0,
        signal,
      );
      if (!valid()) return;
      calls = page.calls;
      before = page.next_before;
      selected = null;
      parts = [];
      current = null;
      text = null;
      loaded = true;
    });
  }
  function open(call: CapturedCall, offset = 0) {
    return perform(async (signal, valid) => {
      const page = await api.contextParts(
        project,
        session,
        call.run,
        call.identity,
        offset,
        signal,
      );
      if (!valid()) return;
      selected = call;
      parts = page.parts;
      after = page.next_after;
      coverage = page.coverage;
      current = null;
      text = null;
    });
  }
  function read(part: ContextPart, offset = 0) {
    const call = selected;
    if (!call) return;
    return perform(async (signal, valid) => {
      const page = await api.contextPartText(
        project,
        session,
        call.run,
        call.identity,
        part.ordinal,
        offset,
        signal,
      );
      if (valid()) {
        current = part;
        text = page;
      }
    });
  }
  onMount(() => {
    void refresh();
  });
  onDestroy(() => {
    generation++;
    controller?.abort();
  });
</script>

<h3>Captured context composition</h3>
<p>
  Inspect the actual visible request captured before a model dispatch, including live and
  interrupted attempts.
</p>
<label
  >Capture scope <select
    value={scope}
    onchange={(event) => {
      scope = event.currentTarget.value as typeof scope;
      void refresh();
    }}
  >
    <option value="attempt">Selected attempt</option><option value="agent"
      >This agent across continuations</option
    >
  </select></label
>
<button type="button" onclick={() => void refresh()} disabled={busy}>Refresh latest captures</button
>
{#if error}<ErrorMessage {error} />{/if}
{#if busy}<p role="status">Loading captured context…</p>{/if}
{#if loaded && !calls.length}<p>
    No dispatch captures or intents are retained for this scope. Older attempts cannot be
    reconstructed exactly.
  </p>{/if}
<ol class="calls">
  {#each calls as call (call.identity)}<li>
      <button
        type="button"
        disabled={busy || !call.captured}
        onclick={() => void open(call)}
        aria-pressed={selected?.identity === call.identity}
      >
        {formatTime(call.at)} · {call.kind} · {call.alias}
      </button>
      <small
        >{call.run} · {call.identity}{#if !call.captured}
          · {call.omitted ?? 'Capture unavailable'}{/if}</small
      >
    </li>{/each}
</ol>
{#if before !== null}<button type="button" disabled={busy} onclick={() => void refresh(true)}
    >Older dispatches</button
  >{/if}
{#if selected}
  <p>{coverage}</p>
  <ol class="parts" start={(parts[0]?.ordinal ?? 0) + 1}>
    {#each parts as part (part.ordinal)}<li>
        <button
          type="button"
          disabled={busy}
          onclick={() => void read(part)}
          aria-pressed={current?.ordinal === part.ordinal}
        >
          {part.section ?? part.kind} · {part.role} · {part.bytes.toLocaleString()} bytes
        </button>
        {#if part.origin?.message}<small
            >Archive {part.origin.message} · position {part.origin.position}</small
          >{/if}
        {#if part.version}<small>Prompt version {part.version}</small>{/if}
        {#if part.native_omitted}<small>Provider-native replay omitted</small>{/if}
      </li>{/each}
  </ol>
  {#if parts[0]?.ordinal}<button
      type="button"
      disabled={busy}
      onclick={() => selected && void open(selected)}>First parts</button
    >{/if}
  {#if after !== null}<button
      type="button"
      disabled={busy}
      onclick={() => selected && void open(selected, after ?? 0)}>Next parts</button
    >{/if}
{/if}
{#if current && text}
  <h4>Part {current.ordinal + 1}: {current.section ?? current.kind}</h4>
  <p>
    Bytes {text.offset + 1}–{text.next_offset ?? text.total} of {text.total.toLocaleString()}. Full
    part SHA-256: <code>{text.sha256}</code>
  </p>
  <pre>{text.text}</pre>
  {#if text.offset}<button
      type="button"
      disabled={busy}
      onclick={() => current && void read(current)}>Start of part</button
    >{/if}
  {#if text.next_offset !== null}<button
      type="button"
      disabled={busy}
      onclick={() => current && void read(current, text?.next_offset ?? 0)}>Next text chunk</button
    >{/if}
{/if}

<style>
  .calls,
  .parts {
    padding-left: 1.5rem;
  }
  li {
    margin-bottom: 0.5rem;
  }
  small {
    display: block;
    overflow-wrap: anywhere;
  }
  button[aria-pressed='true'] {
    font-weight: 700;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 32rem;
    overflow: auto;
    padding: 1rem;
    background: var(--background-secondary, #f4f4f4);
  }
  code {
    overflow-wrap: anywhere;
  }
</style>
