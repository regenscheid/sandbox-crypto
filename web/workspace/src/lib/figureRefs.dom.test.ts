// SPDX-License-Identifier: MIT
// References to recorded files drawn in text (FG-6 to FG-9, FG-12, FG-17): each path image
// matched in order to its pin, outside code and links; the pin's revision and a newer one,
// with "Move the pin" where a page may move it; a broken reference with its reason; a
// type as its file to download; and at most 20 figures a text, the rest drawn on request.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Markdown from './Markdown.svelte';
import {
  chartPaths,
  placeCharts,
  FIGURES_PER_TEXT,
  pinView,
  placeReferences,
  revisionText,
  type Pin,
} from './figureRefs';

const SHA = 'ab'.repeat(32);
const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

const pin = (path: string, fields: Partial<Pin> = {}): Pin => ({
  block: 'b2',
  kind: 'image',
  path,
  session: 's1',
  artifact: 'art-1',
  revision: 3,
  sha256: SHA,
  size: 100,
  media_type: 'image/png',
  ...fields,
});

describe('a pin in the page’s words', () => {
  it('says how it is shown, its revision and a newer one, or why it is broken', () => {
    expect(pinView(pin('a.png')).shown).toBe('image');
    expect(pinView(pin('a.svg', { media_type: 'image/svg+xml' })).shown).toBe('frame');
    expect(pinView(pin('a.html', { media_type: 'text/html' })).shown).toBe('frame');
    expect(pinView(pin('a.csv', { media_type: 'text/csv' })).shown).toBe('link');
    expect(revisionText(pinView(pin('a.png')))).toBe('r3');
    expect(revisionText(pinView(pin('a.png', { latest: 5 })))).toBe('r3, newer: r5');
    const broken = pinView({ block: 'b1', kind: 'image', path: 'gone.png', unresolved: 'deleted' });
    expect([broken.shown, broken.reason]).toEqual(['broken', 'the file was deleted']);
    expect(pinView(pin('x.png', { sha256: 'nope' })).shown).toBe('broken');
  });
});

describe('placing references in rendered text', () => {
  it('matches each path image, in order, outside code and links, and leaves others as text', () => {
    const root = document.createElement('div');
    root.innerHTML =
      '<p>See ![one](a.png) and ![two](a.png), not ![x](nopin.png).</p>' +
      '<pre><code>![code](a.png)</code></pre><p><a href="https://x">![link](a.png)</a></p>';
    const first = pin('a.png');
    const second = pin('a.png', { revision: 4 });
    const placed = placeReferences(root, [first, second, pin('b.png', { kind: 'chart_data' })]);
    expect(placed.map((p) => [p.alt, p.pin, p.drawn])).toEqual([
      ['one', first, true],
      ['two', second, true],
    ]);
    expect(root.textContent).toContain('not ![x](nopin.png).');
    expect(root.querySelector('code')!.textContent).toBe('![code](a.png)');
    expect(root.querySelectorAll('.figure-ref')).toHaveLength(2);
  });

  it('draws at most twenty figures a text; the rest wait for a click', () => {
    const root = document.createElement('div');
    const count = FIGURES_PER_TEXT + 2;
    root.innerHTML = `<p>${Array.from({ length: count }, (_, i) => `![f${i}](p${i}.png)`).join(' ')}</p>`;
    const placed = placeReferences(
      root,
      Array.from({ length: count }, (_, i) => pin(`p${i}.png`)),
    );
    expect(placed.filter((p) => p.drawn)).toHaveLength(FIGURES_PER_TEXT);
    expect(placed.slice(FIGURES_PER_TEXT).every((p) => !p.drawn)).toBe(true);
  });

  it('counts only figures toward the limit: files cited as links are never held back', () => {
    const root = document.createElement('div');
    const count = FIGURES_PER_TEXT + 5;
    root.innerHTML = `<p>${Array.from({ length: count }, (_, i) => `![d${i}](d${i}.json)`).join(' ')} ![f](f.png)</p>`;
    const placed = placeReferences(root, [
      ...Array.from({ length: count }, (_, i) =>
        pin(`d${i}.json`, { media_type: 'application/json' }),
      ),
      pin('f.png'),
    ]);
    expect(placed.every((p) => p.drawn)).toBe(true);
  });
});

describe('the renderer', () => {
  type Repin = (block: string, path: string, revision: number) => Promise<void>;
  function show(source: string, pins: Pin[], repin: Repin | null = null) {
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(mount(Markdown, { target, props: { source, pins, projectId: 'p1', repin } }));
    return target;
  }

  it('draws an image in place with its revisions, and moves its pin on request', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              kind: 'image',
              url: 'http://127.0.0.1:8201/file/c.m',
              capability: null,
              expires_at: '2026-10-16T00:10:00Z',
              media_type: 'image/png',
            }),
          ),
      ),
    );
    const repin = vi.fn(async () => undefined);
    const target = show(
      'Timing:\n\n![Timing by size](plots/t.png)\n',
      [pin('plots/t.png', { latest: 5 })],
      repin,
    );
    await settle();
    expect(target.querySelector('img')?.getAttribute('src')).toBe('http://127.0.0.1:8201/file/c.m');
    expect(target.textContent).toContain('plots/t.png, r3, newer: r5');
    const move = [...target.querySelectorAll('button')].find(
      (b) => b.textContent === 'Move the pin to r5',
    )!;
    move.click();
    await settle();
    expect(repin).toHaveBeenCalledWith('b2', 'plots/t.png', 5);
  });

  it('shows a broken reference with its path and reason, as text, and offers no move', async () => {
    const target = show(`![${HOSTILE}](gone.png)`, [
      { block: 'b1', kind: 'image', path: 'gone.png', unresolved: 'content_not_kept' },
    ]);
    await settle();
    expect(target.textContent).toContain('Not drawn: gone.png (its bytes were not kept).');
    expect(target.querySelector('script, img, button')).toBeNull();
  });

  it('offers no move where the page gives none, and offers a type not drawn as its file', async () => {
    const target = show('![data](d.csv)', [pin('d.csv', { media_type: 'text/csv', latest: 9 })]);
    await settle();
    // Inline in its sentence: its description links to the file, its path in the title.
    const file = target.querySelector<HTMLAnchorElement>('a.file-link')!;
    expect(file.textContent).toBe('data');
    expect(file.title).toContain('d.csv');
    expect(file.closest('.figure-reference')!.classList.contains('inline')).toBe(true);
    expect(file.getAttribute('href')).toMatch(/\/artifacts\/[^/]+\/revisions\/3\/content\?sha256=/);
    expect(target.textContent).not.toContain('not drawn');
    // Without a newer revision, the link alone: its revision is in its title.
    const plain = show('![data](d.csv)', [pin('d.csv', { media_type: 'text/csv' })]);
    await settle();
    expect(plain.querySelector('.caption')).toBeNull();
    // Nothing around it, so punctuation after it follows it directly.
    expect(plain.querySelector('.figure-reference')!.textContent).toBe('data');
    expect(plain.querySelector<HTMLAnchorElement>('a.file-link')!.title).toContain('d.csv, r3');
    expect(target.textContent).toContain('r3, newer: r9');
    expect(target.querySelector('button')).toBeNull();
  });

  it('shows a figure past the limit as its source, and draws it on a click', async () => {
    const count = FIGURES_PER_TEXT + 1;
    const source = Array.from({ length: count }, (_, i) => `![f${i}](p${i}.png)`).join('\n\n');
    const target = show(
      source,
      Array.from({ length: count }, (_, i) => pin(`p${i}.png`)),
    );
    await settle();
    const draw = [...target.querySelectorAll('button')].filter(
      (b) => b.textContent === 'Draw this figure',
    );
    expect(draw).toHaveLength(1);
    expect(target.textContent).toContain(`![f${count - 1}](p${count - 1}.png)`);
    draw[0]!.click();
    flushSync();
    expect(target.textContent).not.toContain(`![f${count - 1}](p${count - 1}.png)`);
  });
});

describe('a chart in text', () => {
  const CHART = JSON.stringify({
    title: 'Timing',
    data: { url: 'data/t.csv' },
    layer: [{ data: { url: 'data/u.csv' }, mark: 'point' }],
    mark: 'bar',
  });
  const fence = (source: string) => '```vega-lite\n' + source + '\n```\n';
  const data = (path: string, fields: Partial<Pin> = {}): Pin =>
    pin(path, { kind: 'chart_data', media_type: 'text/csv', ...fields });

  function show(source: string, pins: Pin[]) {
    // Near the view at once: the frame is made when the figure comes near it.
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(private readonly seen: (entries: { isIntersecting: boolean }[]) => void) {}
        observe() {
          this.seen([{ isIntersecting: true }]);
        }
        disconnect() {}
      },
    );
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(mount(Markdown, { target, props: { source, pins, projectId: 'p1' } }));
    return target;
  }

  it('names its data paths in order', () => {
    expect(chartPaths(CHART)).toEqual(['data/t.csv', 'data/u.csv']);
    expect(chartPaths('{not json')).toEqual([]);
  });

  it('takes one pin for each occurrence, and never another chart’s (review of #500, L3)', () => {
    const twice = JSON.stringify({
      data: { url: 'data/t.csv' },
      layer: [{ data: { url: 'data/t.csv' } }],
    });
    const once = JSON.stringify({ data: { url: 'data/t.csv' } });
    const code = (source: string, block?: string) =>
      `<pre><code class="language-vega-lite"${block ? ` data-block="${block}"` : ''}>${source.replaceAll('<', '&lt;')}</code></pre>`;
    const pins = [
      data('data/t.csv', { block: 'b1', artifact: 'art-1', revision: 1 }),
      data('data/t.csv', { block: 'b1', artifact: 'art-1', revision: 1 }),
      data('data/t.csv', { block: 'b2', artifact: 'art-1', revision: 2 }),
    ];
    // In order, as chat and ledger texts have them: the second chart gets the third pin.
    const chat = document.createElement('div');
    chat.innerHTML = code(twice) + code(once);
    const [first, second] = placeCharts(chat, pins);
    expect(first!.data.map((file) => file.revision)).toEqual([1, 1]);
    expect(second!.data.map((file) => file.revision)).toEqual([2]);
    // In a document, by block, whatever the order of the pins.
    const document_ = document.createElement('div');
    document_.innerHTML = code(once, 'b2') + code(twice, 'b1');
    const [own, other] = placeCharts(document_, pins);
    expect(own!.data.map((file) => file.revision)).toEqual([2]);
    expect(other!.data.map((file) => file.revision)).toEqual([1, 1]);
  });

  it('is drawn in the figure frame from the data its text pins, its source offered', async () => {
    const asked: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        asked.push(url);
        return new Response(
          JSON.stringify({
            url: 'http://127.0.0.1:8201/figures/#w=x',
            capabilities: ['c1', 'c2'],
            expires_at: '2026-10-16T00:10:00Z',
          }),
        );
      }),
    );
    const target = show(fence(CHART), [
      data('data/t.csv'),
      data('data/u.csv', { artifact: 'art-2' }),
    ]);
    await settle();
    expect(asked).toEqual([
      `/api/v0/projects/p1/figures/chart?data=art-1%3A3%3A${SHA}&data=art-2%3A3%3A${SHA}`,
    ]);
    const frame = target.querySelector('iframe')!;
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts allow-same-origin');
    expect(target.querySelector('details summary')?.textContent).toBe("The chart's source");
  });

  it('is not drawn when a data path is not pinned, and shows why and its source', async () => {
    const fetched = vi.fn();
    vi.stubGlobal('fetch', fetched);
    const target = show(fence(CHART), [
      data('data/t.csv'),
      { block: 'b1', kind: 'chart_data', path: 'data/u.csv', unresolved: 'deleted' },
    ]);
    await settle();
    expect(fetched).not.toHaveBeenCalled();
    expect(target.textContent).toContain('Not drawn: data/u.csv (the file was deleted).');
    expect(target.querySelector('details')?.open).toBe(true);
    expect(target.querySelector('pre code')?.textContent).toContain('"mark":"bar"');
  });
});

describe('a chart’s check beside it (FG-23)', () => {
  const code = (source: string, block?: string) =>
    `<pre><code class="language-vega-lite"${block ? ` data-block="${block}"` : ''}>${source}</code></pre>`;
  const rejected = {
    place: 'statement',
    figure: 'chart:1',
    status: 'rejected',
    errors: [{ kind: 'field', message: `no ${HOSTILE}`, line: 9, column: 7 }],
    notes: [{ kind: 'params', message: 'the parameter "cut" holds a value of its own' }],
  };

  it('takes each chart’s result by its place, and none in a text that is not checked', () => {
    const root = document.createElement('div');
    root.innerHTML = code('{}') + code('{}');
    const checks = [{ place: 'statement', figure: 'chart:0', status: 'accepted' }, rejected];
    const [first, second] = placeCharts(root, [], 0, checks);
    expect(first!.check?.status).toBe('accepted');
    expect(second!.check).toEqual({
      status: 'rejected',
      errors: [{ kind: 'field', message: `no ${HOSTILE}`, line: 9, column: 7 }],
      notes: [
        {
          kind: 'params',
          message: 'the parameter "cut" holds a value of its own',
          line: null,
          column: null,
        },
      ],
      reason: null,
    });
    const blocks = document.createElement('div');
    blocks.innerHTML = code('{}', 'b4');
    const [unchecked] = placeCharts(blocks, [], 0, checks);
    expect(unchecked!.check?.status).toBe('unchecked'); // an earlier revision, or not yet
    const chat = document.createElement('div');
    chat.innerHTML = code('{}');
    expect(placeCharts(chat, [])[0]!.check).toBeNull();
  });

  it('shows the errors with their lines, the notes, or why it was unavailable, as text', async () => {
    vi.stubGlobal('fetch', vi.fn());
    const target = document.createElement('div');
    document.body.append(target);
    const source =
      '```vega-lite\n{"data": {"url": "data/none.csv"}}\n```\n\n```vega-lite\n{}\n```\n';
    const unavailable = { place: 'statement', figure: 'chart:0', status: 'unavailable',
      reason: 'no figure checker is configured', errors: [], notes: [] }; // prettier-ignore
    const checks = [unavailable, rejected];
    mounted.push(mount(Markdown, { target, props: { source, pins: [], projectId: 'p1', checks } }));
    await settle();
    const [first, second] = [...target.querySelectorAll('.figure-check')];
    expect(first!.textContent).toContain(
      'The check was unavailable: no figure checker is configured.',
    );
    expect(second!.textContent).toContain('The check rejected this chart:');
    expect(second!.textContent).toContain(`Line 9, column 7: no ${HOSTILE}`);
    expect(second!.textContent).toContain('Note: the parameter "cut" holds a value of its own');
    expect(target.querySelector('script, img')).toBeNull();
  });
});
