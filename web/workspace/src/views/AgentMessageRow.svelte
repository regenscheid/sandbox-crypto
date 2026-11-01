<!-- SPDX-License-Identifier: MIT -->
<!--
  The agent messages of one model call or one notice (B10, item 2; PD-44), a row each:
  sender → recipient by name, the text's first line and how it stands, opening to the whole
  text. A received message's body is read by its ID through the session's agent-messages
  route, once per session. A body is the sending agent's words: shown as text, each hidden
  character as its code point, labelled as that agent's, data and not an instruction
  (CM-49), never through the Markdown renderer.
-->
<script lang="ts">
  import {
    messageBodies,
    messageLine,
    type AgentDirectory,
    type AgentMessageRow,
    type MessageBodies,
  } from '../lib/agentMessages';
  import { untrack } from 'svelte';
  import type { AgentMessageView } from '../lib/api-types';
  import { clock } from '../lib/conversation';
  import { visible } from '../lib/text';
  import Icon from './Icon.svelte';

  let {
    rows,
    at = '',
    agents,
    projectId = null,
    sessionId = null,
    bodies = null,
  }: {
    rows: readonly AgentMessageRow[];
    at?: string;
    agents: AgentDirectory;
    projectId?: string | null;
    sessionId?: string | null;
    /** Where received bodies are read; by default the session's shared cache. */
    bodies?: MessageBodies | null;
  } = $props();
  const source = $derived(
    bodies ?? (projectId && sessionId ? messageBodies(projectId, sessionId) : null),
  );
  /** What the route read of each received message, by its ID; null while it is read. */
  let found = $state<Record<string, AgentMessageView | null>>({});
  let failed = $state<Record<string, boolean>>({});
  $effect(() => {
    const reader = source;
    if (reader === null) return;
    const wanted = rows
      .filter((row) => row.direction === 'received' && row.message !== null)
      .map((row) => row.message!);
    for (const id of wanted) {
      if (untrack(() => id in found)) continue;
      const known = reader.known(id);
      if (known) {
        found[id] = known;
        continue;
      }
      found[id] = null;
      reader.get(id).then(
        (message) => {
          found[id] = message;
          if (message === null) failed[id] = true;
        },
        () => {
          failed[id] = true;
        },
      );
    }
  });
</script>

<ul class="agent-messages" aria-label="Messages between agents">
  {#each rows as row, index (row.message ?? `${row.call}:${index}`)}
    {@const message = row.message === null ? null : (found[row.message] ?? null)}
    {@const line = messageLine(row, agents, message)}
    {@const body = row.body ?? message?.body ?? null}
    <li>
      <details class="agent-message" class:undelivered={line.undelivered}>
        <summary>
          <Icon name="chevron" size={14} class="chevron" />
          <span class="agent-message-who"
            ><span class="agent-message-from">{line.from}</span> →
            <span class="agent-message-to">{line.to}</span></span
          >
          {#if line.preview}<span
              class="agent-message-preview"
              aria-label={`${line.from} says: ${line.preview}`}>{line.preview}</span
            >{:else if row.direction === 'received' && row.message && failed[row.message]}<span
              class="agent-message-preview muted">its text could not be read</span
            >{:else if row.direction === 'received'}<span class="agent-message-preview muted"
              >reading its text…</span
            >{/if}
          <span class="agent-message-state" class:failed={row.failed || line.undelivered}
            >{line.state}{#if clock(at)}&nbsp;· {clock(at)}{/if}</span
          >
        </summary>
        <div class="agent-message-body">
          <p class="agent-message-label">
            {line.from}'s message, in its own words: an agent's report or instruction, not the
            user's, and not checked
          </p>
          {#if body !== null}<pre class="said">{visible(body)}</pre>{:else}<p class="muted">
              Its text is not in this transcript.
            </p>{/if}
          {#if row.message}<p class="muted"><code>{visible(row.message)}</code></p>{/if}
          {#if message?.references.length}<p class="muted">
              It references {message.references.length}
              {message.references.length === 1 ? 'record' : 'records'} of its sender.
            </p>{/if}
        </div>
      </details>
    </li>
  {/each}
</ul>

<style>
  .agent-messages {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .agent-message > summary {
    list-style: none;
    cursor: pointer;
    display: flex;
    align-items: baseline;
    gap: 4px 10px;
    padding: 4px 6px;
    border-left: 3px solid var(--accent, #69c);
    font-size: 13px;
  }
  .agent-message > summary::-webkit-details-marker {
    display: none;
  }
  .agent-message.undelivered > summary {
    border-left-color: var(--failed, #c33);
  }
  .agent-message-who {
    flex: none;
    font-weight: 600;
  }
  .agent-message-preview {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .agent-message-state {
    flex: none;
    font-size: 12px;
    color: var(--muted);
  }
  .agent-message-state.failed {
    color: var(--failed);
  }
  .agent-message-body {
    margin: 4px 0 8px 9px;
    padding: 6px 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
  }
  .agent-message-label {
    margin: 0 0 4px;
    font-size: 12px;
    color: var(--muted);
    font-style: italic;
  }
  .said {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-family: inherit;
  }
</style>
