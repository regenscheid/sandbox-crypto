<!-- SPDX-License-Identifier: MIT -->
<!-- Inspected MCP traffic (docs/POLICY-MIDDLEWARE.md, "MCP traffic inspection"): for each
     call, the middleware's decision, the recorded arguments, the answer's head and excerpt,
     the patterns found and what inspection did with the answer. Arguments, excerpts and
     findings were written by the agent or a server and redacted by the middleware: they
     show as text, every hidden character a code point. -->
<script lang="ts">
  import type { TrafficCall } from '../lib/api-types';
  import { formatTime } from '../lib/format';
  import { answerDecision, answerHead, callDecision } from '../lib/mcpTraffic';
  import { sizeText } from '../lib/pdfAnchors';
  import { visible } from '../lib/text';

  let { calls, note = null }: { calls: readonly TrafficCall[]; note?: string | null } = $props();
</script>

<div class="traffic">
  {#if note}<p class="muted">{note}</p>{/if}
  {#each calls as call (call.seq)}
    <article class="traffic-call" aria-label={`Inspected call ${call.seq}`}>
      <div class="traffic-head">
        <span class="muted">{formatTime(call.at)}</span>
        <code>{visible(call.server ?? 'unknown server')}</code>
        <code>{visible(call.method ?? '')}{call.tool ? ` ${visible(call.tool)}` : ''}</code>
      </div>
      <p class="traffic-decision" class:problem={call.decision.outcome === 'refused'}>
        {visible(callDecision(call))}{#if call.decision.permission_request !== null}
          · permission request {call.decision.permission_request}{/if}
      </p>
      {#if call.arguments}
        <h5>Arguments as recorded</h5>
        <pre class="traffic-text">{visible(call.arguments.text)}</pre>
        <p class="muted">
          {sizeText(call.arguments.bytes)}{call.arguments.truncated
            ? ', cut at the recorded bound'
            : ''}
          · SHA-256 <code>{call.arguments.sha256.slice(0, 16)}…</code>
        </p>
      {:else}
        <p class="muted">No arguments were recorded.</p>
      {/if}
      {#if call.answer}
        {@const answer = call.answer}
        <h5>Answer</h5>
        <p>{visible(answerHead(answer))}</p>
        <p class="traffic-decision" class:problem={answer.decision.action !== 'passed'}>
          {visible(answerDecision(answer))}
        </p>
        {#if answer.excerpt !== null}
          <pre class="traffic-text">{visible(answer.excerpt)}</pre>
          {#if answer.excerpt_truncated}<p class="muted">Excerpt cut at the recorded bound.</p>{/if}
        {/if}
        {#if answer.note}<p class="muted">{visible(answer.note)}</p>{/if}
        {#if answer.findings.length}
          <h5>Findings</h5>
          <ul class="traffic-findings">
            {#each answer.findings as finding, index (index)}
              <li>
                <span class="pill" class:failed={finding.action === 'block'}
                  >{visible(finding.name)} · {finding.action}</span
                >
                <span class="muted">at character {finding.offset}</span>
                <pre class="traffic-text">{visible(finding.excerpt)}</pre>
              </li>
            {/each}
          </ul>
          {#if answer.more_findings}<p class="muted">
              And {answer.more_findings} more not recorded.
            </p>{/if}
        {/if}
      {:else if call.decision.outcome === 'admitted'}
        <p class="muted">No answer recorded (yet).</p>
      {/if}
    </article>
  {/each}
</div>

<style>
  .traffic-call {
    border-top: 1px solid var(--line, #ddd);
    padding: 0.4rem 0;
  }
  .traffic-head {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    align-items: baseline;
  }
  .traffic-text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 16rem;
    overflow: auto;
  }
  .traffic-findings {
    list-style: none;
    padding: 0;
  }
  h5 {
    margin: 0.4rem 0 0.2rem;
    font-size: 0.85rem;
  }
</style>
