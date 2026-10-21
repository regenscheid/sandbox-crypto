// SPDX-License-Identifier: MIT
// Applying a retained or suggested patch (LD-19, LD-20; threat model, A3): the user sees
// the text after it exactly as it would be saved, each hidden character a code point, with
// its differences from the current revision, before applying it.
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import PatchPreviewView from '../views/PatchPreviewView.svelte';
import type { PatchPreview } from './api';
import { exactViews, HIDDEN_CONTENT, hostileMarkup } from './approvalTesting';
import { hiddenCharacters, visible } from './text';

function preview(proposed: string | null, current = 'First.\n\nSecond.\n'): PatchPreview {
  return {
    patch: {
      patch_id: 'patch-1',
      author: { kind: 'agent', session_id: 's', run_id: 'r1', role: 'main' },
      operations: [{ op: 'replace', block: 'b2', markdown: 'x' }],
    },
    base: 3,
    current,
    proposed,
    allowed: proposed !== null,
    message: null,
    unnamed: [],
  } as unknown as PatchPreview;
}

function shown(value: PatchPreview): string {
  return render(PatchPreviewView, {
    props: { preview: value, busy: false, onapply: () => undefined, onclose: () => undefined },
  }).body;
}

describe('a patch the user applies', () => {
  it.each(HIDDEN_CONTENT)('shows every character of a patch with %s', (_, text) => {
    const proposed = `First.\n\n${text}\n`;
    const html = shown(preview(proposed));
    const [exact, changes] = exactViews(html);
    expect(exact).toBe(visible(proposed));
    expect(hiddenCharacters(exact!)).toEqual([]);
    expect(changes).toContain(visible(text).split('\n')[0]);
    expect(hostileMarkup(html)).toBe(false);
    expect(html).toContain('exactly as it would be saved');
  });

  it('shows the change word by word, not two whole texts side by side', () => {
    const html = shown(preview('First.\n\nSecond, always.\n', 'First.\n\nSecond, never.\n'));
    expect(html).toContain('<del>never.</del>');
    expect(html).toContain('<ins>always.</ins>');
    expect(html).not.toContain('Current text');
  });

  it('says when a patch cannot apply, and offers nothing to apply', () => {
    const html = shown(preview(null));
    expect(html).toContain('cannot safely apply');
    expect(html).toMatch(/<button[^>]*disabled[^>]*>Apply reviewed patch/);
  });

  it('shows the retained operations with hidden characters as code points', () => {
    const value = preview('First.\n\nx\n');
    const override = String.fromCodePoint(0x202e);
    value.patch.operations = [{ op: 'replace', block: 'b2', markdown: `a${override}b` }];
    const html = shown(value);
    expect(hiddenCharacters(html)).toEqual([]);
    expect(html).toContain('a⟦U+202E⟧b');
  });

  it('shows reasons in the history as code points too', async () => {
    const source = (await import('../views/HistoryView.svelte?raw')).default;
    expect(source).toContain('{visible(item.reason)}');
    expect(source).toContain('{visible(patch.reason)}');
  });

  it('renders no text as HTML', () => {
    const views = import.meta.glob(
      [
        '../views/PatchPreviewView.svelte',
        '../views/PatchReview.svelte',
        '../views/HistoryView.svelte',
      ],
      {
        query: '?raw',
        import: 'default',
        eager: true,
      },
    );
    expect(Object.keys(views)).toHaveLength(3);
    for (const source of Object.values(views)) expect(String(source)).not.toContain('{@html');
  });
});
