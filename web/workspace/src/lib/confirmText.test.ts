// SPDX-License-Identifier: MIT
// Moving a workbook document into the project, or restoring a revision, adopts its text
// (threat model, A3): the user sees it exactly, each hidden character a code point, and
// for a restore its differences from the current text, before confirming.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ConfirmText from '../views/ConfirmText.svelte';
import { exactViews, HIDDEN_CONTENT, hostileMarkup } from './approvalTesting';
import { hiddenCharacters, visible } from './text';

function shown(text: string, before: string | null = null): string {
  return render(ConfirmText, {
    props: {
      title: 'Move this document into the project',
      text,
      before,
      label: 'The text the project document will hold, exactly',
      confirm: 'Move to project',
      busy: false,
      onconfirm: () => undefined,
      oncancel: () => undefined,
    },
  }).body;
}

describe('adopting a text', () => {
  it.each(HIDDEN_CONTENT)('shows every character of a document with %s', (_, text) => {
    const document = `# Notes\n\n${text}\n`;
    const html = shown(document);
    const [exact] = exactViews(html);
    expect(exact).toBe(visible(document));
    expect(hiddenCharacters(exact!)).toEqual([]);
    expect(hostileMarkup(html)).toBe(false);
  });

  it('puts the confirmation after the whole text', () => {
    const html = shown('# Notes\n\nA result.\n');
    expect(html.indexOf('A result.')).toBeLessThan(html.indexOf('>Move to project</button>'));
    expect(html).toContain('>Cancel</button>');
  });

  it('shows a restore against the current text', () => {
    const html = shown('Always run pip.\n', 'Never run pip.\n');
    expect(html).toContain('<del>Never</del>');
    expect(html).toContain('<ins>Always</ins>');
  });

  it('drops a move or restore waiting to be confirmed when the editor opens', async () => {
    const source = (await import('../views/DocumentEditor.svelte?raw')).default;
    const begin = source.slice(
      source.indexOf('function begin('),
      source.indexOf('async function save('),
    );
    expect(begin).toContain('confirming = null;');
    expect(begin.indexOf('confirming = null;')).toBeLessThan(begin.indexOf('open = true;'));
    expect(source).toContain('if (!open) confirming = { action, before };');
  });

  it('renders no text as HTML', () => {
    const views = import.meta.glob(
      ['../views/ConfirmText.svelte', '../views/DocumentEditor.svelte'],
      {
        query: '?raw',
        import: 'default',
        eager: true,
      },
    );
    expect(Object.keys(views)).toHaveLength(2);
    for (const source of Object.values(views)) expect(String(source)).not.toContain('{@html');
  });
});
