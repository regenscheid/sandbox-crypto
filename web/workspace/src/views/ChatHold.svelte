<!-- SPDX-License-Identifier: MIT -->
<!-- A hold of the session's outward calls (D103): a detection rule saw a burst, hammering,
     enumeration or a streak of refusals, and the policy middleware paused the session's
     HTTP, git and MCP calls. Sandbox compute goes on. The card says what the rule saw and
     lists the latest calls; Clear resumes the calls, Stop session stops its run. Every
     text is remote and shown as text. -->
<script lang="ts">
  import type { PermissionDecision } from '../lib/api';
  import type { PendingApproval } from '../lib/api-types';
  import { askedBy, holdSummary } from '../lib/permissionCards';
  import { idle, submit } from '../lib/submit';
  import { visible } from '../lib/text';
  import ErrorMessage from './ErrorMessage.svelte';
  import { adminBlock } from '../lib/viewer.svelte';

  let {
    request,
    answer,
  }: {
    request: PendingApproval;
    answer: (decision: PermissionDecision) => Promise<void>;
  } = $props();
  const sending = $state(idle());
  /** Why this user may not clear the hold (D104); null when they may. */
  const blocked = $derived(adminBlock());
  const calls = $derived(request.hold?.requests ?? []);
  async function send(decision: PermissionDecision) {
    await submit(sending, () => answer(decision));
  }
</script>

<section class="hold" aria-label="The session's outward calls are paused">
  <h3>Outward calls paused</h3>
  <p>{visible(holdSummary(request))}</p>
  <p class="muted">
    Calls by {askedBy(request)}{#if request.rule}
      (<code>{visible(request.rule)}</code>){/if}. Sandbox compute goes on; network and connector
    calls wait until you clear the hold.
  </p>
  {#if calls.length}
    <details>
      <summary>Latest calls</summary>
      <ul class="calls">
        {#each calls as call, index (index)}
          <li class="mono">{visible(`${call.method} ${call.host} ${call.target}`)}</li>
        {/each}
      </ul>
    </details>
  {/if}
  <div class="hold-actions">
    <button
      type="button"
      class="primary-button"
      disabled={sending.busy || blocked !== null}
      title={blocked ?? undefined}
      onclick={() => void send('clear')}>Clear hold</button
    >
    <button type="button" disabled={sending.busy} onclick={() => void send('stop')}
      >Stop session</button
    >
  </div>
  {#if blocked}<p class="admin-only">{blocked} You can stop the session.</p>{/if}
  {#if sending.error}<ErrorMessage error={sending.error} />{/if}
</section>

<style>
  .hold {
    border: 1px solid var(--border-strong);
    border-radius: 14px;
    padding: 14px 16px;
    margin-top: 22px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    background: var(--surface);
  }
  h3 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    text-transform: none;
    letter-spacing: normal;
    color: var(--text);
  }
  p {
    margin: 0;
  }
  .calls {
    margin: 6px 0 0;
    padding-left: 18px;
    overflow-wrap: anywhere;
  }
  .hold-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
</style>
