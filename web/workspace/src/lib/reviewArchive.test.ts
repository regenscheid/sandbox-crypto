// SPDX-License-Identifier: MIT
// A review by the session's archive (CR-2, CR-14 as the context management spec amends
// them): the earlier runs' calls a run's view held, shown apart as context, each under its
// run with its outcome, a refused one marked; and each item that names an archive place
// leading to the archived message, which a read-only view shows whole.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ReviewActions from '../views/ReviewActions.svelte';
import { hostileMarkup } from './approvalTesting';
import { anchorView } from './researchState';
import { actionsOf } from './reviewActions';
import { archivedLink, describeItem } from './reviewContext';
import { messageHref, parseRoute } from './router';

const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';
const ID = 'msg-0123456789abcdef';

describe('a review by the archive', () => {
  it("shows earlier runs' calls apart, as context, each under its run, a refused one marked", () => {
    const payload = {
      actions: [{ tool: 'python', target: {}, outcome: 'ok', made: [], run: 'run-2' }],
      total: 1,
      documents: [],
      earlier_runs: {
        about: 'earlier runs',
        actions: [
          {
            tool: 'sage',
            target: {},
            outcome: 'ok',
            made: ['execution exe-1 (succeeded)'],
            run: 'run-1',
          },
          {
            tool: 'document_patch',
            target: { document: HOSTILE },
            outcome: 'refused',
            made: [],
            run: 'run-1',
          },
        ],
        total: 2,
        shown: 2,
      },
    };
    const parsed = actionsOf(JSON.stringify(payload))!;
    const runs = parsed.earlierRuns!.runs;
    expect(runs.map((group) => group.run)).toEqual(['run-1']);
    expect(runs[0]!.actions.map((action) => [action.tool, action.outcome])).toEqual([
      ['sage', 'ok'],
      ['document_patch', 'refused'],
    ]);
    expect(parsed.earlier).toBeNull();
    expect(
      actionsOf(JSON.stringify({ ...payload, earlier_runs: undefined }))!.earlierRuns,
    ).toBeNull();
    const html = render(ReviewActions, {
      props: { projectId: 'p1', sessionId: 's1', actions: parsed },
    }).body;
    const context = html.indexOf('Earlier runs, as context');
    expect(context).toBeGreaterThan(html.indexOf('python'));
    expect(html.indexOf('document_patch')).toBeGreaterThan(context);
    expect(html).toContain('<strong class="problem');
    expect(html).toContain('refused</strong>');
    expect(html.replace(/\s+/g, ' ')).toContain(
      'A refused or failed call is no record that it happened.',
    );
    expect(html).toContain('href="#/projects/p1/sessions/s1/runs/run-1"');
    expect(hostileMarkup(html)).toBe(false);
  });

  it('leads from an item that names an archive place to the archived message', () => {
    const transcript = describeItem('p1', 's1', {
      kind: 'transcript',
      locator: { run: 'run-2', invocation: 't', message: 3, archive: { id: ID, position: 41 } },
    })!;
    expect(transcript.mark).toBe(
      "A message of the reviewed run, as the session's archive keeps it:",
    );
    expect(transcript.links).toEqual([
      { text: 'the archived message, position 41', href: `#/projects/p1/messages/${ID}` },
    ]);
    expect(
      describeItem('p1', 's1', { kind: 'transcript', locator: { run: 'run-2', message: 3 } }),
    ).toBeNull();
    const execution = describeItem('p1', 's1', {
      kind: 'execution',
      locator: {
        execution: 'exe-1',
        run: 'run-c',
        delegation: 'del-1',
        archive: { id: ID, position: 7 },
      },
    })!;
    expect(execution.links).toContainEqual({
      text: 'the tool result it came back as',
      href: `#/projects/p1/messages/${ID}`,
    });
    expect(archivedLink('p1', { archive: { id: `msg-${HOSTILE}`, position: 1 } })).toBeNull();
    expect(archivedLink('p1', {})).toBeNull();
  });

  it('routes to one archived message, and a provenance anchor leads to it too', () => {
    expect(parseRoute(`#/projects/p1/messages/${ID}`)).toEqual({
      view: 'message',
      project: 'p1',
      message: ID,
    });
    expect(parseRoute('#/projects/p1/messages/msg-1')).toEqual({ view: 'missing' });
    expect(messageHref('p1', ID)).toBe(`#/projects/p1/messages/${ID}`);
    expect(anchorView('p1', { message: ID, kind: 'tool_result' }).href).toBe(messageHref('p1', ID));
    expect(anchorView('p1', { message: 'msg-1', kind: 'tool_result' }).href).toBeNull();
  });
});
