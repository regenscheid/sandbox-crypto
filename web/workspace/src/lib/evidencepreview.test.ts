// SPDX-License-Identifier: MIT
import { describe, it, expect } from 'vitest';
import { render } from 'svelte/server';
import PinnedEvidencePreview from '../views/PinnedEvidencePreview.svelte';

describe('raw pinned evidence preview', () => {
  it('shows successful empty source outside the still-empty source element', () => {
    const html = render(PinnedEvidencePreview, { props: { text: '' } }).body;
    expect(html).toContain('Empty source (0 characters).');
    expect(html).toMatch(/<pre[^>]*aria-label="Pinned evidence preview"[^>]*><\/pre>/);
  });
  it('escapes untrusted source without Markdown or HTML interpretation', () => {
    const html = render(PinnedEvidencePreview, {
      props: { text: '<script>run()</script>\n**raw**' },
    }).body;
    expect(html).toContain('&lt;script>run()&lt;/script>');
    expect(html).toContain('**raw**');
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('Empty source');
  });
});
