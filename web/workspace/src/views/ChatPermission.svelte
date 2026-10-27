<!-- SPDX-License-Identifier: MIT -->
<!-- A request that waits for the user's answer (D76, D97): a connector tool call the
     policy middleware holds (D98), or research code's network or package request. The card shows a
     structured summary (the tool or endpoint, the destination, the effect class and the
     significant arguments, or method, host, path, content type and size), the agent's own
     words (marked as the agent's), and for a delegated agent the brief its parent gave it
     (D99); the raw arguments or a JSON body's top-level fields fold away. Allow offers only
     the scopes the request's ceiling allows, which the server checks again; Deny refuses.
     Every text is shown as text. -->
<script lang="ts">
  import type { PermissionDecision } from '../lib/api';
  import type { PendingApproval } from '../lib/api-types';
  import { clock, firstLine } from '../lib/conversation';
  import { askedBy, cardTitle, scopeChoices, summaryRows } from '../lib/permissionCards';
  import { idle, submit } from '../lib/submit';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';
  import Icon from './Icon.svelte';

  let {
    request,
    call = null,
    answer,
  }: {
    request: PendingApproval;
    /** The held call's arguments and its step's words, from the transcript. */
    call?: { arguments: string; text: string } | null;
    answer: (decision: PermissionDecision) => Promise<void>;
  } = $props();
  const scopes = $derived(scopeChoices(request));
  let scope = $state<string>('once');
  let menu = $state(false);
  const sending = $state(idle());
  const chosen = $derived(scopes.find((item) => item.id === scope) ?? scopes[0]!);
  /** The asking agent's own words: a delegated agent's from its request's record, a
   * package request's from the provisioning operation. */
  const words = $derived(
    request.child
      ? request.child.words
      : request.kind === 'packages'
        ? (request.packages?.purpose ?? '')
        : (call?.text ?? ''),
  );
  const rows = $derived(summaryRows(request));
  const fields = $derived(Object.entries(request.http?.fields ?? {}));
  const pretty = $derived.by(() => {
    if (!call?.arguments) return '';
    try {
      return JSON.stringify(JSON.parse(call.arguments), null, 2);
    } catch {
      return call.arguments;
    }
  });
  async function send(decision: PermissionDecision) {
    menu = false;
    await submit(sending, () => answer(decision));
  }
</script>

<svelte:window
  onpointerdown={(event) => {
    if (menu && !(event.target as Element).closest?.('.allow-split')) menu = false;
  }}
/>
<section class="permission" aria-label="A request waiting for your approval">
  <div class="permission-head">
    <h3>{visible(cardTitle(request))}</h3>
    <span class="chips"
      >{#if request.kind === 'mcp'}<span class="chip">{visible(request.server)}</span><span
          class="chip mono">{visible(request.tool)}</span
        >{:else}<span class="chip">{request.kind === 'network' ? 'Network' : 'Packages'}</span
        >{/if}{#if request.effect}<span class="chip effect">{visible(request.effect)}</span
        >{/if}</span
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
  {:else if request.caller_kind !== 'agent'}
    <p class="asked-by">Asked by {askedBy(request)}.</p>
  {/if}
  {#if rows.length}
    <dl class="summary">
      {#each rows as [label, value], index (index)}
        <dt>{label}</dt>
        <dd class:mono={label !== 'Effect'}>{visible(value)}</dd>
      {/each}
    </dl>
  {/if}
  {#if words.trim()}
    <p class="said">{firstLine(words, 400)}</p>
    <small class="muted">Agent-supplied reason.</small>
  {/if}
  {#if request.kind === 'mcp'}
    <details>
      <summary>Arguments <span class="muted mono">({visible(request.tool)})</span></summary>
      {#if pretty}<pre>{visible(pretty)}</pre>{:else}<p class="muted">
          The transcript does not show this call's arguments yet.
        </p>{/if}
    </details>
  {:else if fields.length}
    <details>
      <summary>Body fields <span class="muted">(top level, long values cut)</span></summary>
      <dl class="summary">
        {#each fields as [name, value] (name)}
          <dt class="mono">{visible(name)}</dt>
          <dd class="mono">{visible(value)}</dd>
        {/each}
      </dl>
    </details>
  {/if}
  <p class="muted terms">
    The policy asks you about this {request.kind === 'mcp' ? 'tool' : 'request'}{#if request.rule}
      (<code>{visible(request.rule)}</code>){/if}. Unanswered, it is {request.on_timeout === 'deny'
      ? 'refused'
      : 'let through with an alert'} at {clock(request.expires_at)}.
  </p>
  <div class="permission-actions">
    <div class="allow-split">
      <button
        type="button"
        class="primary-button allow"
        class:alone={scopes.length < 2}
        disabled={sending.busy}
        onclick={() => void send(chosen.id)}
        >Allow <span class="scope">{chosen.id === 'once' ? 'once' : chosen.label}</span></button
      >{#if scopes.length > 1}<button
          type="button"
          class="primary-button caret"
          aria-label="Choose how long to allow"
          aria-haspopup="menu"
          aria-expanded={menu}
          disabled={sending.busy}
          onclick={() => (menu = !menu)}><Icon name="down" size={14} /></button
        >{/if}
      {#if menu}
        <div class="scope-menu" role="menu">
          {#each scopes as item (item.id)}
            <button
              type="button"
              role="menuitemradio"
              aria-checked={chosen.id === item.id}
              onclick={() => {
                scope = item.id;
                menu = false;
              }}
              ><span class="check"
                >{#if chosen.id === item.id}<Icon name="check" size={14} />{/if}</span
              ><span><strong>{item.label}</strong><small>{visible(item.detail)}</small></span
              ></button
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
  .summary {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 2px 12px;
    margin: 0;
    font-size: 13px;
  }
  .summary dt {
    color: var(--muted);
  }
  .summary dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .effect {
    font-weight: 500;
  }
  .allow.alone {
    border-radius: 8px;
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
