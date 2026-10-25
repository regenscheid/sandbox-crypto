// SPDX-License-Identifier: MIT
// A batch's rows (B10, item 1): a call's purpose is the agent's words, labelled as such and
// shown as text, each hidden character as its code point (CM-49); a row without one shows
// its tool and main argument; the gloss is on the right; rows are closed by default.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import WorkGroup from '../views/WorkGroup.svelte';
import type { StepCall, WorkStep } from './conversation';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
});

const row = (more: Partial<StepCall>): StepCall => ({
  id: 'c1',
  name: 'shell',
  arguments: '{"script": "ls data"}',
  done: true,
  outcome: 'succeeded, exit code 0',
  result: 'a\nb',
  gloss: {
    outcome: 'ok',
    exit_code: 0,
    lines: 2,
    bytes: 4,
    files: [],
    more_files: 0,
    error: null,
  },
  ...more,
});

function show(calls: StepCall[], live = false): HTMLElement {
  const steps: WorkStep[] = [{ seq: 1, at: '2026-10-25T10:00:00Z', text: '', calls }];
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(WorkGroup, {
      target,
      props: { from: '2026-10-25T10:00:00Z', to: '2026-10-25T10:01:00Z', steps, live },
    }),
  );
  flushSync();
  return target;
}

describe('a batch of rows', () => {
  it("shows a purpose as the agent's words, as text, with hidden characters as code points", () => {
    const target = show([row({ purpose: 'Reading <img src=x> the law‮ now' })]);
    const purpose = target.querySelector('.row-purpose')!;
    expect(purpose.textContent).toBe('Reading <img src=x> the law⟦U+202E⟧ now');
    expect(purpose.getAttribute('aria-label')).toBe(
      'The agent says: Reading <img src=x> the law⟦U+202E⟧ now',
    );
    expect(target.querySelector('img')).toBeNull();
    expect(target.querySelector('.row-says')!.textContent).toBe('says');
    expect(target.querySelector('.row-gloss')!.textContent).toBe('ok · 2 lines');
    expect(target.querySelector('.step-text')!.textContent).toBe('Ran 1 command · 1 step');
  });

  it('shows the tool and its main argument without a purpose, rows closed by default', () => {
    const target = show([
      row({}),
      row({
        id: 'c2',
        gloss: {
          outcome: 'failed',
          exit_code: 1,
          lines: 1,
          bytes: 13,
          files: [],
          more_files: 0,
          error: 'No such file',
        },
      }),
    ]);
    const labels = [...target.querySelectorAll('.row-label')].map((label) => label.textContent);
    expect(labels).toEqual(['shellls data', 'shellls data']);
    const glosses = [...target.querySelectorAll('.row-gloss')];
    expect(glosses.map((gloss) => gloss.textContent)).toEqual([
      'ok · 2 lines',
      'failed · exit 1 · 1 line · "No such file"',
    ]);
    expect(glosses[1]!.classList.contains('failed')).toBe(true);
    const rows = [...target.querySelectorAll<HTMLDetailsElement>('details.call-row')];
    expect(rows.map((details) => details.open)).toEqual([false, false]);
    // Opened, a row still has the call's arguments and result as text.
    expect(target.querySelector('.call-args')!.textContent).toBe('{"script": "ls data"}');
    expect(target.querySelector('.call-result')!.textContent).toBe('a\nb');
  });

  it('shows the latest calls as rows in the live card, a running one with its time', () => {
    const target = show([row({ purpose: 'Counting the orbits' }), row({ done: false })], true);
    const items = [...target.querySelectorAll('.live-calls li')];
    expect(items[0]!.querySelector('.row-purpose')!.textContent).toBe('Counting the orbits');
    expect(items[1]!.querySelector('.pill.running')!.textContent).toMatch(/^running \d+:\d\d/);
  });
});
