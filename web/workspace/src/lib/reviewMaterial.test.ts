// SPDX-License-Identifier: MIT
// What a review's snapshot holds besides the window's material, each labelled as context
// (CR-2, CR-14 as amended for lab 42): the session plan at dispatch, a delegated run's
// actions and the executions chosen for its outcome, with how many it held and left out,
// an execution's saved output, and the run's earlier windows, apart, as background.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ReviewActions from '../views/ReviewActions.svelte';
import { hostileMarkup } from './approvalTesting';
import { actionsOf } from './reviewActions';
import { describeItem, heldText } from './reviewContext';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

describe("a review's material, labelled", () => {
  it('says the plan item is the plan at dispatch, as context', () => {
    const item = { kind: 'plan', locator: { plan: 'plan-1', version: 3, status_revision: 2 } };
    const view = describeItem('p1', 's1', item)!;
    expect(view.mark).toBe(
      'The session plan when the review was dispatched, as context, not reviewed material:',
    );
    expect(view.text).toBe('plan plan-1 version 3');
    expect(view.links).toEqual([{ text: 'the session', href: '#/projects/p1/sessions/s1' }]);
  });

  it("names a delegated run's executions and actions as context, and its saved outputs", () => {
    const located = { run: 'run-c', delegation: 'del-1', for_run: 'run-a', message: 4 };
    const execution = describeItem('p1', 's1', {
      kind: 'execution',
      locator: { execution: 'exec-9', attempt: 'att-1', ...located },
    })!;
    expect(execution.mark).toBe(
      "An execution of delegation del-1's run, chosen for its outcome, as context:",
    );
    expect(execution.text).toBe('execution exec-9');
    const action = describeItem('p1', 's1', { kind: 'action', locator: located })!;
    expect(action.mark).toBe("What delegation del-1's run did, as context:");
    expect(action.text).toBe('its call 4');
    const output = describeItem('p1', 's1', {
      kind: 'artifact',
      locator: {
        path: '.sandbox/outputs/o.txt',
        delegated: located,
        output_of: { execution: 'exec-9', stream: 'stdout' },
      },
    })!;
    expect(output.mark).toBe(
      "The saved output of an execution of delegation del-1's run, cut short in it:",
    );
    expect(output.text).toBe('stdout of execution exec-9');
  });

  it("says how many of a delegated run's executions the review holds, and how chosen", () => {
    const outcome = {
      delegation: 'del-1',
      executions: { held: 3, left_out: 40, chosen: `the latest ${HOSTILE}` },
    };
    const text = heldText(JSON.stringify(outcome));
    expect(text).toContain("This review holds 3 of its run's executions and left 40 out");
    expect(text).toContain(', chosen: the latest');
    expect(heldText(JSON.stringify({ delegation: 'del-1' }))).toBe('');
    expect(heldText('not JSON')).toBe('');
  });

  it("shows the run's earlier windows apart, as background, not under review", () => {
    const action = (tool: string, run: string) => ({
      tool,
      target: { entry: 'C-1' },
      outcome: 'ok',
      made: [],
      run,
    });
    const payload = {
      actions: [action('ledger_change', 'run-a')],
      total: 1,
      documents: [],
      earlier: {
        about: 'earlier',
        actions: [action('plan_update', 'run-a'), action('document_patch', 'run-a')],
        total: 5,
        shown: 2,
        documents: [],
      },
    };
    const parsed = actionsOf(JSON.stringify(payload))!;
    expect(parsed.earlier!.runs[0]!.actions.map((a) => a.tool)).toEqual([
      'plan_update',
      'document_patch',
    ]);
    expect(parsed.earlier!.total).toBe(5);
    expect(actionsOf(JSON.stringify({ ...payload, earlier: undefined }))!.earlier).toBeNull();
    const item = { kind: 'actions', locator: { from: 1, to: 9, earlier: { from: 1, to: 4 } } };
    expect(describeItem('p1', 's1', item)!.text).toContain(
      '; with, apart, the run’s earlier windows, as background',
    );
    const html = render(ReviewActions, {
      props: { projectId: 'p1', sessionId: 's1', actions: parsed },
    }).body;
    const window = html.indexOf('ledger_change');
    const background = html.indexOf('Earlier windows, as background');
    expect(window).toBeGreaterThan(-1);
    expect(background).toBeGreaterThan(window);
    expect(html.indexOf('plan_update')).toBeGreaterThan(background);
    expect(html).toContain("The first 2 of those windows' 5 calls are listed.");
    expect(hostileMarkup(html)).toBe(false);
  });
});
