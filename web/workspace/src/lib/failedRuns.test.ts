// SPDX-License-Identifier: MIT
import { render } from 'svelte/server';
import { expect, it } from 'vitest';
import type { RunRecord } from './api';
import { entries, describeResult } from './transcript';
import RunView from '../views/RunView.svelte';

const partial: RunRecord = {
  run_id: 'failed', context: {project_id: 'p', session_id: 's', environment_id: 'e', policy_revision: null, actor: 'agent'},
  task: 'Compute', started_at: '2026-10-12T13:00:00Z', ended_at: '2026-10-12T13:00:01Z',
  stopped: 'error', failure: 'tool', final_text: null,
  messages: [{role: 'user', content: 'Compute'}, {role: 'assistant', content: 'Partial observation', tool_calls: [
    {id: 'a', type: 'function', function: {name: 'python', arguments: '{}'}},
    {id: 'b', type: 'function', function: {name: 'shell', arguments: '{}'}},
  ]}, {role: 'tool', tool_call_id: 'a', content: '{"state":"succeeded","stdout":"42"}'}],
  tools: [{tool: 'python', arguments: '{}'}],
};

it('shows failure and partial evidence, with an incomplete call and no final answer', () => {
  const html = render(RunView, {props: {run: partial}}).body;
  expect(html).toContain('Failed run');
  expect(html).toContain('(tool)');
  expect(html).toContain('Partial transcript. No final answer was recorded.');
  expect(html).toContain('Partial observation');
  expect(html).toContain('no result recorded');
  const observed = entries(partial).at(-1);
  expect(observed?.kind).toBe('assistant');
  if (observed?.kind !== 'assistant') throw new Error('Missing partial reply');
  expect(observed.final).toBe(false);
  expect(observed.tools[1]?.record).toBeUndefined();
  expect(describeResult(observed.tools[1]?.result).summary).toBe('no result recorded');
});

it('still renders older records without failure metadata', () => {
  const {failure: _failure, ...legacy} = partial;
  const html = render(RunView, {props: {run: {...legacy, stopped: 'answered', messages: [{role: 'assistant', content: 'Done'}], tools: []}}}).body;
  expect(html).toContain('with an answer');
  expect(html).not.toContain('Failed run');
});
