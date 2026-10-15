// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import type { RunRecord } from './api';
import { argumentFields, describeResult, entries, usageLine } from './transcript';

function run(overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: 'run-1',
    context: {
      project_id: 'p1',
      session_id: 'a',
      environment_id: 'env-1',
      policy_revision: null,
      actor: 'agent',
    },
    task: 'Square 41 + 1',
    started_at: '2026-10-11T13:01:02Z',
    ended_at: '2026-10-11T13:01:06Z',
    stopped: 'answered',
    final_text: 'It is $1764$.',
    messages: [
      { role: 'system', content: 'You are a research assistant.' },
      { role: 'user', content: 'Square 41 + 1' },
      {
        role: 'assistant',
        content: 'Computing.',
        tool_calls: [
          {
            id: 'call-1',
            type: 'function',
            function: { name: 'python', arguments: '{"code": "x = 41"}' },
          },
          {
            id: 'call-2',
            type: 'function',
            function: { name: 'shell', arguments: '{"script": "echo"}' },
          },
        ],
      },
      { role: 'tool', tool_call_id: 'call-2', content: '{"state": "failed", "exit_code": 1}' },
      { role: 'tool', tool_call_id: 'call-1', content: '{"state": "succeeded"}' },
      { role: 'assistant', content: 'It is $1764$.' },
    ],
    tools: [
      { tool: 'python', arguments: '{"code": "x = 41"}', replayed: false },
      { tool: 'shell', arguments: '{"script": "echo"}', replayed: true },
    ],
    ...overrides,
  };
}

describe('entries', () => {
  it('pairs each tool call with its record and its result', () => {
    const [system, user, working, answer] = entries(run());
    expect(system).toEqual({ kind: 'system', text: 'You are a research assistant.' });
    expect(user).toEqual({ kind: 'user', text: 'Square 41 + 1' });
    expect(working).toMatchObject({ kind: 'assistant', text: 'Computing.', final: false });
    if (working?.kind !== 'assistant') throw new Error('not an assistant message');
    expect(working.tools.map((tool) => [tool.name, tool.result, tool.record?.replayed])).toEqual([
      ['python', '{"state": "succeeded"}', false],
      ['shell', '{"state": "failed", "exit_code": 1}', true],
    ]);
    expect(answer).toEqual({ kind: 'assistant', text: 'It is $1764$.', tools: [], final: true });
  });

  it('marks no answer when the run hit its turn limit', () => {
    const found = entries(run({ stopped: 'turn_limit' }));
    expect(found.some((entry) => entry.kind === 'assistant' && entry.final)).toBe(false);
  });
});

describe('arguments', () => {
  it('shows code as a block and other values inline', () => {
    expect(argumentFields('{"code": "x = 1", "timeout_seconds": 30, "cwd": "a\\nb"}')).toEqual([
      { name: 'code', text: 'x = 1', block: true },
      { name: 'timeout_seconds', text: '30', block: false },
      { name: 'cwd', text: 'a\nb', block: true },
    ]);
  });

  it('shows arguments that are not a JSON object as written', () => {
    expect(argumentFields('{"code": ')).toEqual([
      { name: 'arguments', text: '{"code": ', block: true },
    ]);
  });
});

describe('results', () => {
  it('reads an execution', () => {
    const view = describeResult(
      JSON.stringify({
        state: 'succeeded',
        exit_code: 0,
        stdout: 'hello\n',
        outputs: ['42'],
        platform_notices: ['admitted example.org'],
        replayed: 'this call was made before',
        ran_on: { sandbox: 'ss-1', host: 'vm', isolation_tier: 3 },
        attempt_id: 'att-1',
        written: ['out.csv'],
      }),
    );
    expect([view.summary, view.tone]).toEqual(['succeeded, exit 0', 'ok']);
    expect(view.streams).toEqual([
      { name: 'stdout', text: 'hello\n', block: true },
      { name: 'output 1', text: '42', block: true },
    ]);
    expect(view.notices).toEqual(['admitted example.org', 'this call was made before']);
    expect(view.facts).toEqual(['ran on sandbox ss-1, host vm, isolation tier 3', 'attempt att-1']);
    expect(view.rest).toBe('{\n  "written": [\n    "out.csv"\n  ]\n}');
  });

  it('says when isolation was not measured', () => {
    const view = describeResult('{"state": "succeeded", "ran_on": {"host": "h"}}');
    expect(view.facts).toEqual(['ran on host h, isolation unmeasured']);
    expect(view.rest).toBeNull();
  });

  it('reads a failed command and a tool error', () => {
    expect(describeResult('{"state": "succeeded", "exit_code": 2}').tone).toBe('failed');
    const error = describeResult(
      '{"error": {"kind": "not_found", "message": "no file", "traceback": "t"}}',
      { tool: 'read_file', arguments: '{}', error: 'not_found' },
    );
    expect([error.summary, error.tone, error.error]).toEqual([
      'error: not_found',
      'failed',
      { kind: 'not_found', message: 'no file', traceback: 't' },
    ]);
  });

  it('shows a result that is not JSON as written', () => {
    const view = describeResult('example.org:443 reachable');
    expect([view.text, view.summary]).toEqual(['example.org:443 reachable', 'done']);
    expect(describeResult(undefined).summary).toBe('no result recorded');
  });
});

describe("a run's model usage", () => {
  it('sums the calls and their tokens, naming each model once', () => {
    expect(
      usageLine([
        { model: 'model-a', input_tokens: 1000, output_tokens: 200 },
        { model: 'model-a', input_tokens: 200, output_tokens: 140 },
      ]),
    ).toBe('2 model calls (model-a), 1,200 input and 340 output tokens');
  });
  it('says when the gateway left counts out, rather than counting them as zero', () => {
    expect(usageLine([{ model: null, input_tokens: 5, output_tokens: null }])).toBe(
      '1 model call, 5 input and 0 output tokens, some counts not reported by the gateway',
    );
  });
  it('adds what the gateway said the calls cost, and invents none', () => {
    const call = { model: 'm', input_tokens: 10, output_tokens: 2 };
    expect(
      usageLine([
        { ...call, cost: 0.0125 },
        { ...call, cost: 0.5 },
      ]),
    ).toBe('2 model calls (m), 20 input and 4 output tokens, $0.51');
    expect(usageLine([{ ...call, cost: 0.00042 }, { ...call, cost: null }, call])).toBe(
      '3 model calls (m), 30 input and 6 output tokens, $0.00042 for the 1 calls the gateway gave a cost for',
    );
    expect(usageLine([call])).toBe('1 model call (m), 10 input and 2 output tokens');
  });
  it('says nothing for a record with no usage, as older records have', () => {
    expect(usageLine(undefined)).toBeNull();
    expect(usageLine([])).toBeNull();
  });
});
