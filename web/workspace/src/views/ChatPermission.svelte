<!-- SPDX-License-Identifier: MIT -->
<!-- A connector tool call the MCP proxy holds for the user's answer (D76): what it asks,
     the agent's own words for why (marked as the agent's), its arguments from the
     session's transcript, and Allow, once, for the project, or for any escalated tool of
     its server, or Deny. The policy decides which calls are held, so no answer here
     reaches a tool it denies. A delegated agent's call says so, with the brief its parent
     gave it and its own words (D99). Every text is shown as text. -->
<script lang="ts">
  import type { PendingApproval } from '../lib/api-types';
  import { clock, firstLine } from '../lib/conversation';
  import { idle, submit } from '../lib/submit';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';
  import Icon from './Icon.svelte';

  type Decision = 'once' | 'project' | 'server' | 'deny';
  let {
    request,
    call = null,
    answer,
  }: {
    request: PendingApproval;
    /** The held call's arguments and its step's words, from the transcript. */
    call?: { arguments: string; text: string } | null;
    answer: (decision: Decision) => Promise<void>;
  } = $props();
  let scope = $state<'once' | 'project' | 'server'>('once');
  let menu = $state(false);
  const sending = $state(idle());
  const scopes = $derived([
    { id: 'once' as const, label: 'Once', detail: 'This call only' },
    {
      id: 'project' as const,
      label: 'This project',
      detail: 'This tool, remembered for the project',
    },
    {
      id: 'server' as const,
      label: `Any tool in ${visible(request.server)}`,
      detail: 'Every tool the policy asks about, for the project',
    },
  ]);
  const chosen = $derived(scopes.find((item) => item.id === scope)!);
  /** The asking agent's own words: a delegated agent's from its request's record. */
  const words = $derived(request.child ? request.child.words : (call?.text ?? ''));
  const pretty = $derived.by(() => {
    if (!call?.arguments) return '';
    try {
      return JSON.stringify(JSON.parse(call.arguments), null, 2);
    } catch {
      return call.arguments;
    }
  });
  async function send(decision: Decision) {
    menu = false;
    await submit(sending, () => answer(decision));
  }
</script>

<svelte:window
  onpointerdown={(event) => {
    if (menu && !(event.target as Element).closest?.('.allow-split')) menu = false;
  }}
/>
<section class="permission" aria-label="A tool call waiting for your approval">
  <div class="permission-head">
    <h3>Use {visible(request.tool)}?</h3>
    <span class="chips"
      ><span class="chip">{visible(request.server)}</span><span class="chip mono"
        >{visible(request.tool)}</span
      ></span
    >
  </div>
  {#if request.child}
    <p class="asked-by">
      Asked by a delegated agent ({visible(request.child.kind)}), not the main agent.
    </p>
    {#if request.child.brief.trim()}
      <details class="brief">
        <summary>Its brief</summary>
        <p class="said">{visible(request.child.brief)}</p>
        <small class="muted">Written by the agent that delegated it.</small>
      </details>
    {/if}
  {/if}
  {#if words.trim()}
    <p class="said">{firstLine(words, 400)}</p>
    <small class="muted">Agent-supplied reason.</small>
  {/if}
  <details>
    <summary>Arguments <span class="muted mono">({visible(request.tool)})</span></summary>
    {#if pretty}<pre>{visible(pretty)}</pre>{:else}<p class="muted">
        The transcript does not show this call's arguments yet.
      </p>{/if}
  </details>
  <p class="muted terms">
    The policy asks you about this tool{#if request.rule}
      (<code>{visible(request.rule)}</code>){/if}. Unanswered, it is {request.on_timeout === 'deny'
      ? 'refused'
      : 'let through with an alert'} at {clock(request.expires_at)}.
  </p>
  <div class="permission-actions">
    <div class="allow-split">
      <button
        type="button"
        class="primary-button allow"
        disabled={sending.busy}
        onclick={() => void send(scope)}
        >Allow <span class="scope">{chosen.id === 'once' ? 'once' : chosen.label}</span></button
      ><button
        type="button"
        class="primary-button caret"
        aria-label="Choose how long to allow"
        aria-haspopup="menu"
        aria-expanded={menu}
        disabled={sending.busy}
        onclick={() => (menu = !menu)}><Icon name="down" size={14} /></button
      >
      {#if menu}
        <div class="scope-menu" role="menu">
          {#each scopes as item (item.id)}
            <button
              type="button"
              role="menuitemradio"
              aria-checked={scope === item.id}
              onclick={() => {
                scope = item.id;
                menu = false;
              }}
              ><span class="check"
                >{#if scope === item.id}<Icon name="check" size={14} />{/if}</span
              ><span><strong>{item.label}</strong><small>{item.detail}</small></span></button
            >
          {/each}
        </div>
      {/if}
    </div>
    <button type="button" disabled={sending.busy} onclick={() => void send('deny')}>Deny</button>
  </div>
  {#if sending.error}<ErrorMessage error={sending.error} />{/if}
</section>

<style>
  .permission {
    border: 1px solid var(--border-strong);
    border-radius: 14px;
    padding: 14px 16px;
    margin-top: 22px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    background: var(--surface);
    box-shadow: 0 1px 2px rgb(23 52 72 / 5%);
  }
  .permission-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 12px;
  }
  h3 {
    flex: 1 1 auto;
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    text-transform: none;
    letter-spacing: normal;
    color: var(--text);
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chip {
    padding: 2px 8px;
    border-radius: 6px;
    background: var(--subtle);
    font-size: 12px;
    color: var(--body-text);
  }
  .mono {
    font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  }
  .said {
    margin: 0;
    white-space: pre-wrap;
  }
  .asked-by {
    margin: 0;
    font-size: 13px;
    color: var(--body-text);
  }
  .brief .said {
    margin-top: 6px;
  }
  details summary {
    cursor: pointer;
    font-size: 13px;
  }
  details pre {
    margin: 8px 0 0;
    max-height: 16rem;
  }
  .terms {
    margin: 0;
    font-size: 12px;
  }
  .permission-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-top: 4px;
  }
  .allow-split {
    position: relative;
    display: flex;
  }
  .allow {
    border-radius: 8px 0 0 8px;
  }
  .scope {
    font-weight: 400;
    opacity: 0.85;
  }
  .caret {
    border-radius: 0 8px 8px 0;
    border-left: 1px solid rgb(255 255 255 / 25%);
    padding: 0 8px;
  }
  .scope-menu {
    position: absolute;
    left: 0;
    bottom: 40px;
    min-width: 260px;
    padding: 6px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface);
    box-shadow: var(--shadow);
    display: flex;
    flex-direction: column;
    gap: 2px;
    z-index: 8;
  }
  .scope-menu button {
    display: flex;
    gap: 8px;
    align-items: flex-start;
    min-height: 0;
    padding: 8px 10px;
    border: 0;
    background: transparent;
    text-align: left;
  }
  .scope-menu button:hover {
    background: var(--subtle);
  }
  .scope-menu strong {
    display: block;
    font-weight: 500;
  }
  .scope-menu small {
    display: block;
    color: var(--muted);
    font-size: 12px;
  }
  .check {
    width: 14px;
    flex: none;
    color: var(--accent);
    padding-top: 2px;
  }
</style>
