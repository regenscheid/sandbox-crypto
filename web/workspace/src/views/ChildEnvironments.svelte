<!-- SPDX-License-Identifier: MIT -->
<!-- An open session's child environments (CE-8, CE-11): each delegated run's own
     environment, with the brief's title, the delegation that owns it, its state, what
     holds it, its hand-back and how it is kept or released. With `kernels`, a ready one
     also shows its kernels, as the Compute tab does. Briefs, paths and the worker's
     messages may quote agents' text, shown with what a person may not see as its code
     point. -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import {
    age,
    childTitle,
    handBackText,
    holdText,
    observable,
    phaseText,
    readChildEnvironments,
    type ChildEnvironmentListing,
  } from '../lib/childEnvironments';
  import type { KernelInfo, KernelObservation } from '../lib/kernels';
  import { initialPoll, Poller, type PollState } from '../lib/poller';
  import { visible } from '../lib/text';
  import KernelPanel from './KernelPanel.svelte';
  import PollStatus from './PollStatus.svelte';

  let {
    projectId,
    sessionId,
    kernels = false,
    detail,
    onobserve,
    heading = 'Child environments',
  }: {
    projectId: string;
    sessionId: string;
    /** Show each ready one's kernels (the Compute tab). */
    kernels?: boolean;
    detail?: Snippet<[KernelInfo]>;
    /** Told of each child environment's kernel observation, by its ID. */
    onobserve?: (environmentId: string, state: PollState<KernelObservation>) => void;
    heading?: string;
  } = $props();

  let view = $state<PollState<ChildEnvironmentListing>>(initialPoll());
  let now = $state(Date.now());
  $effect(() => {
    const project = projectId,
      session = sessionId;
    view = initialPoll();
    return new Poller(
      async (signal) => {
        const found = await readChildEnvironments(project, session, signal);
        now = Date.now();
        return found;
      },
      (value) => {
        view = value;
      },
      { intervalMs: 10_000 },
    ).watch();
  });
  const environments = $derived(view.data?.environments ?? []);
</script>

{#if view.phase !== 'unsupported'}<section aria-label={heading} class="child-environments">
    <h3>{heading}</h3>
    <PollStatus phase={view.phase} hasData={view.data !== null} what="Child environment views" />
    {#if view.data && environments.length === 0}
      <p class="muted">No delegated run of this session has had an environment of its own.</p>
    {/if}
    {#each environments as child (child.environment_id)}
      <article class="agent-card child-environment" data-environment={child.environment_id}>
        <div class="agent-heading">
          <strong>{childTitle(child)}</strong>
          <span class="pill">{child.kind}</span>
          <span class="pill {child.phase}">{phaseText(child)}</span>
        </div>
        <p class="agent-detail">
          <code>{child.environment_id}</code> · delegation <code>{child.delegation_id}</code>
          · profile <code>{visible(child.profile)}</code>
          {#if child.isolation_tier !== null}· tier {child.isolation_tier}{/if}
          · created {age(child, now)} ago
        </p>
        <p class="child-hold">{holdText(child)}</p>
        <p class="child-hand-back">{handBackText(child)}</p>
        {#if child.hand_back && child.hand_back.left.length}
          <ul class="child-left">
            {#each child.hand_back.left as file, index (index)}
              <li><code>{visible(file.path)}</code>: {visible(file.reason)}</li>
            {/each}
          </ul>
        {/if}
        {#if child.message}<p class="muted child-message">{visible(child.message)}</p>{/if}
        {#if kernels && child.phase === 'creating'}
          <p class="muted">Being created: its kernels start once it is ready.</p>
        {:else if kernels && observable(child)}
          <KernelPanel
            {projectId}
            {sessionId}
            environmentId={child.environment_id}
            child
            heading={`Kernels of ${childTitle(child)}`}
            onobserve={(state) => onobserve?.(child.environment_id, state)}
            {detail}
          />
        {/if}
      </article>
    {/each}
  </section>{/if}

<style>
  .child-environments {
    margin-top: 16px;
  }
  .child-left {
    margin: 4px 0;
    overflow-wrap: anywhere;
  }
</style>
