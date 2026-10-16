// SPDX-License-Identifier: MIT
// The figure frame in the workspace's page (FG-14, FG-17, FG-18): an SVG is framed with the
// sandbox PV-2 uses and posted its capability, to the preview origin only; a status counts
// only from that frame's window and that origin; an HTML file is a frame with no scripts;
// at most eight draw at once, and a figure not drawn in time is removed and named. And
// what a library drew is made inert before it is shown.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import FigureFrame from '../views/FigureFrame.svelte';
import { sanitized } from '../figures/sanitize';
import { Slots } from './figures';

const SHA = 'ab'.repeat(32);
const PREVIEW = 'http://127.0.0.1:8201';
const CAP = `${'a'.repeat(40)}.${'b'.repeat(43)}`;
const HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(1)>';

/** A framed page's window: the tests never load the preview origin's pages. */
const framed = { postMessage: vi.fn() } as unknown as Window;

const mounted: ReturnType<typeof mount>[] = [];
afterEach(() => {
  vi.restoreAllMocks();
  for (const component of mounted.splice(0)) unmount(component);
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function settle(): Promise<void> {
  for (let i = 0; i < 8; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

function serve(link: Record<string, unknown>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            expires_at: '2026-10-16T00:10:00Z',
            capability: null,
            media_type: 'x',
            ...link,
          }),
        ),
    ),
  );
}

/** An observer that says at once whether the figure is near the view. */
function near(visible: boolean) {
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      constructor(private readonly seen: (entries: { isIntersecting: boolean }[]) => void) {}
      observe() {
        this.seen([{ isIntersecting: visible }]);
      }
      disconnect() {}
    },
  );
}

function show(props: Record<string, unknown> = {}, visible = true) {
  near(visible);
  vi.spyOn(HTMLIFrameElement.prototype, 'contentWindow', 'get').mockReturnValue(framed);
  const target = document.createElement('div');
  document.body.append(target);
  mounted.push(
    mount(FigureFrame, {
      target,
      props: {
        projectId: 'p1',
        artifact: 'art-1',
        revision: 3,
        sha256: SHA,
        alt: 'plot.svg',
        ...props,
      },
    }),
  );
  return target;
}

const status = (fields: Record<string, unknown> = {}) => ({
  type: 'figure-status',
  status: 'rendered',
  width: 600,
  height: 320,
  code: '',
  ...fields,
});

describe('an SVG in the figure frame', () => {
  it('is framed with the viewer sandbox, posted its capability, and sized by its own status', async () => {
    serve({
      kind: 'svg',
      url: `${PREVIEW}/figures/#w=x`,
      capability: CAP,
      media_type: 'image/svg+xml',
    });
    const target = show();
    await settle();
    const frame = target.querySelector('iframe')!;
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts allow-same-origin');
    expect(frame.getAttribute('referrerpolicy')).toBe('no-referrer');
    expect(frame.getAttribute('src')).toBe(`${PREVIEW}/figures/#w=x`);
    frame.dispatchEvent(new Event('load'));
    expect(vi.mocked(framed.postMessage).mock.calls).toEqual([
      [{ type: 'figure', kind: 'svg', source: '', capabilities: [CAP] }, PREVIEW],
    ]);
    // Another window, another origin or another shape is ignored.
    window.dispatchEvent(
      new MessageEvent('message', { data: status(), origin: PREVIEW, source: window }),
    );
    window.dispatchEvent(
      new MessageEvent('message', {
        data: status(),
        origin: 'http://127.0.0.1:8200',
        source: framed,
      }),
    );
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { ...status(), x: 1 },
        origin: PREVIEW,
        source: framed,
      }),
    );
    flushSync();
    expect(frame.style.height).toBe('0px');
    window.dispatchEvent(
      new MessageEvent('message', { data: status(), origin: PREVIEW, source: framed }),
    );
    flushSync();
    expect(target.querySelector('iframe')!.style.height).toBe('320px');
  });

  it('shows the path and the reason, as text, when the frame says it failed', async () => {
    serve({ kind: 'svg', url: `${PREVIEW}/figures/#w=x`, capability: CAP });
    const target = show({ alt: HOSTILE });
    await settle();
    const frame = target.querySelector('iframe')!;
    window.dispatchEvent(
      new MessageEvent('message', {
        data: status({ status: 'error', code: 'size' }),
        origin: PREVIEW,
        source: framed,
      }),
    );
    flushSync();
    expect(target.querySelector('iframe, script, img')).toBeNull();
    expect(target.textContent).toContain(`Not drawn: ${HOSTILE}`);
  });

  it('is removed, and named, when it is not drawn in time', async () => {
    serve({ kind: 'svg', url: `${PREVIEW}/figures/#w=x`, capability: CAP });
    const target = show({ limit: 20 });
    await settle();
    expect(target.querySelector('iframe')).not.toBeNull();
    await new Promise((resolve) => setTimeout(resolve, 40));
    flushSync();
    expect(target.querySelector('iframe')).toBeNull();
    expect(target.textContent).toContain('not drawn within 0.02 seconds');
  });

  it('frames nothing the server names on another scheme', async () => {
    serve({ kind: 'svg', url: 'javascript:alert(1)', capability: CAP });
    const target = show();
    await settle();
    expect(target.querySelector('iframe')).toBeNull();
    expect(target.textContent).toContain('no usable frame');
  });
});

describe('a frame far from the view', () => {
  it('is not made, and nothing is minted, until the figure comes near', async () => {
    const asked = vi.fn(async () => new Response('{}'));
    vi.stubGlobal('fetch', asked);
    const target = show({}, false);
    await settle();
    expect(asked).not.toHaveBeenCalled();
    expect(target.querySelector('iframe')).toBeNull();
    expect(target.textContent).toContain('Waiting to draw');
  });
});

describe('an HTML file', () => {
  it('is the page of a frame with no scripts, at a fixed height', async () => {
    serve({ kind: 'html', url: `${PREVIEW}/file/${CAP}`, media_type: 'text/html' });
    const target = show({ alt: 'table.html' });
    await settle();
    const frame = target.querySelector('iframe')!;
    expect(frame.getAttribute('sandbox')).toBe('');
    expect(frame.getAttribute('src')).toBe(`${PREVIEW}/file/${CAP}`);
    expect(frame.style.height).toBe('400px');
    frame.dispatchEvent(new Event('load'));
    flushSync();
    expect(target.textContent).not.toContain('Not drawn');
  });
});

describe('the frames a page draws at once', () => {
  it('are at most the limit; the others wait their turn, in order', async () => {
    const slots = new Slots(2);
    const first = await slots.acquire();
    const second = await slots.acquire();
    const order: number[] = [];
    const third = slots.acquire().then((done) => (order.push(3), done));
    const fourth = slots.acquire().then((done) => (order.push(4), done));
    await settle();
    expect(order).toEqual([]);
    first();
    first(); // ending a turn twice frees one turn only
    await settle();
    expect(order).toEqual([3]);
    second();
    await settle();
    expect(order).toEqual([3, 4]);
    (await third)();
    (await fourth)();
  });
});

describe('what a library drew', () => {
  const NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"';
  // Review of #492, L1: each bypass the reviewer listed, and the old sanitizer's cases.
  const BYPASSES: [string, RegExp][] = [
    [`<svg ${NS}><set attributeName="href" to="javascript:alert(1)"/></svg>`, /<set|javascript/i],
    [`<svg ${NS}><a id="l"><text>x</text></a><animate xlink:href="#l" attributeName="href" values="javascript:alert(1)"/></svg>`, /<animate|javascript|<a\b/i],
    [`<svg ${NS}><animateMotion dur="1s"/><animateTransform attributeName="transform"/></svg>`, /animate/i],
    [`<svg ${NS}><rect style="fill:url(https://evil.example/x)"/></svg>`, /evil|url\(/i],
    [`<svg ${NS}><rect fill="url('https://evil.example/x')" style="fill:red;background:url(&quot;https://evil.example&quot;)"/></svg>`, /evil|url/i],
    [`<svg ${NS}><style>@import url(https://evil.example/x.css);</style></svg>`, /<style|@import|evil/i],
    [`<svg ${NS}><rect style="fill:\\75 rl(https://evil.example)"/></svg>`, /evil/i],
    [`<svg ${NS}><text srcset="https://evil.example/a.png 1x" poster="https://evil.example/p.png" background="https://evil.example/b.png">x</text></svg>`, /srcset|poster|background|evil/i],
    [`<svg ${NS}><foreignObject><form action="https://evil.example"><button formaction="https://evil.example">x</button></form></foreignObject></svg>`, /action|evil|<form|foreignObject/i],
    [`<svg ${NS}><foreignObject><meta http-equiv="refresh" content="0;url=https://evil.example"/></foreignObject></svg>`, /<meta|evil|foreignObject/i],
    [`<svg ${NS} onload="x()"><a href="https://evil.example" xlink:href="javascript:x()"><text onclick="y()">label</text></a></svg>`, /href|onload|onclick|<a\b|evil/i],
    [`<svg ${NS}><image href="https://evil.example/i.png"/><use href="https://evil.example/s.svg#x"/><feImage href="https://evil.example"/></svg>`, /<image|<use|feImage|evil/i],
    [`<svg ${NS}><script>x()</script><foreignObject><iframe src="https://evil.example"></iframe></foreignObject></svg>`, /script|foreignObject|iframe|evil/i],
    [`<svg ${NS}><link rel="stylesheet" href="https://evil.example"/><base href="https://evil.example/"/></svg>`, /<link|<base|evil/i],
    [`<svg ${NS}><!-- note --><?xml-stylesheet href="https://evil.example"?><rect/></svg>`, /note|xml-stylesheet|evil/i],
    ['<html><body><img src="https://evil.example"/></body></html>', /img|evil|html/i],
    ['<svg><rect/></svg>', /rect/i], // no SVG namespace: not an SVG, nothing kept
  ]; // prettier-ignore

  it.each(BYPASSES)('drops what could link, load or run: %s', (markup, gone) => {
    const holder = document.createElement('div');
    holder.append(sanitized(markup, document));
    expect(holder.innerHTML).not.toMatch(gone);
  });

  it('keeps a drawing, its plain styles and its references within itself', () => {
    const drawn = sanitized(
      `<svg ${NS} viewBox="0 0 10 10"><defs><linearGradient id="g"><stop offset="0" stop-color="#36c"/></linearGradient></defs>` +
        '<g aria-label="bars"><rect width="4" height="8" fill="url(#g)" style="fill:#36c"/><text x="1" y="9">label</text></g></svg>',
      document,
    );
    const holder = document.createElement('div');
    holder.append(drawn);
    const rect = holder.querySelector('rect')!;
    expect(rect.ownerDocument).toBe(document);
    expect(rect.getAttribute('style')).toBe('fill:#36c');
    expect(rect.getAttribute('fill')).toBe('url(#g)');
    expect(holder.textContent).toContain('label');
    expect(holder.querySelector('g')?.getAttribute('aria-label')).toBe('bars');
    expect(holder.querySelector('stop')?.getAttribute('stop-color')).toBe('#36c');
  });
});
