// SPDX-License-Identifier: MIT
/**
 * The figure frame's page (docs/specs/figures.md, FG-14), served only by the preview
 * origin under /figures/. It takes one request from the workspace's window, fetches only
 * that figure's capabilities from its own origin, draws it, and answers with its size and
 * a status. An SVG file is decoded as an image, so it runs no script and loads nothing,
 * whatever it holds, and painted once onto a canvas, so nothing in it animates.
 */
import {
  accepted,
  frameWorkspace,
  MAX_FILE_BYTES,
  statusMessage,
  type FigureCode,
  type FigureRequest,
} from './protocol';
import { Refusal } from './chart';
import { chartSvg } from './vegaChart';
import { sanitized } from './sanitize';
import { framedBy } from '../viewer/anchor';
import './figures.css';

const figure = document.getElementById('figure') as HTMLElement;
const workspace = frameWorkspace(location.hash);
let taken = false;

/** The largest figure drawn, in CSS pixels a side and canvas pixels in all. */
const MAX_SIDE = 4096;
const MAX_CANVAS_PIXELS = 2 ** 24;

function answer(status: 'rendered' | 'error', code: FigureCode = ''): void {
  if (workspace === null) return;
  const box = figure.getBoundingClientRect();
  const height = Math.max(box.height, document.documentElement.scrollHeight);
  window.parent.postMessage(statusMessage(status, box.width, height, code), workspace);
}

async function file(capability: string, type: string): Promise<Blob> {
  const response = await fetch(`/file/${capability}`, {
    credentials: 'omit',
    cache: 'no-store',
    redirect: 'error',
  });
  if (!response.ok) throw new Refusal(response.status === 413 ? 'size' : 'refused');
  if (!(response.headers.get('Content-Type') ?? '').startsWith(type)) throw new Refusal('type');
  const data = await response.blob();
  if (data.size > MAX_FILE_BYTES) throw new Refusal('size');
  return new Blob([data], { type });
}

/**
 * An SVG drawn once onto a canvas of bounded size (review of #492, M1): decoded as an
 * image, which runs no script and fetches nothing, then painted, so no animation runs and
 * the browser never repaints it. The canvas is fitted to the frame's width, each side at
 * most 4,096 CSS pixels, and its pixels at most 2^24 at the screen's ratio.
 */
async function drawSvg(request: FigureRequest): Promise<void> {
  const blob = await file(request.capabilities[0]!, 'image/svg+xml');
  const image = new Image();
  const url = URL.createObjectURL(blob);
  try {
    image.src = url;
    await image.decode().catch(() => {
      throw new Refusal('unreadable');
    });
    const natural = [image.naturalWidth || 300, image.naturalHeight || 150] as const;
    const width = Math.min(natural[0], document.body.clientWidth || natural[0], MAX_SIDE);
    const height = Math.min(MAX_SIDE, (natural[1] * width) / natural[0]);
    if (!(width > 0 && height > 0)) throw new Refusal('size');
    const ratio = Math.min(
      window.devicePixelRatio || 1,
      Math.sqrt(MAX_CANVAS_PIXELS / (width * height)),
    );
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.floor(width * ratio));
    canvas.height = Math.max(1, Math.floor(height * ratio));
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    const context = canvas.getContext('2d');
    if (context === null) throw new Refusal('unreadable');
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    figure.replaceChildren(canvas);
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** A data file's text, from its capability, as a chart reads it. */
async function dataText(capability: string): Promise<string> {
  const response = await fetch(`/file/${capability}`, {
    credentials: 'omit',
    cache: 'no-store',
    redirect: 'error',
  });
  if (!response.ok) throw new Refusal(response.status === 413 ? 'size' : 'refused');
  return response.text();
}

/** A chart (FG-15, chart.ts), its drawing sanitized before it is shown; its accessible
 * name is its description or title (FG-18). */
async function drawChart(request: FigureRequest): Promise<void> {
  const files = new Map(request.paths.map((path, index) => [path, request.capabilities[index]!]));
  const { svg, name } = await chartSvg(request.source, files, dataText);
  const drawn = sanitized(svg, document);
  if (name !== null) figure.setAttribute('aria-label', name);
  figure.replaceChildren(drawn);
}

function draw(request: FigureRequest): Promise<void> {
  return request.kind === 'vega-lite' ? drawChart(request) : drawSvg(request);
}

if (workspace === null || !framedBy(workspace, location.ancestorOrigins)) {
  figure.textContent = 'This frame draws a figure only inside the workspace that made its link.';
} else {
  window.addEventListener('message', (event) => {
    if (taken) return;
    const request = accepted(event, workspace, window.parent);
    if (request === null) return;
    taken = true; // one figure a frame: a second request, from anyone, is ignored
    draw(request).then(
      () => answer('rendered'),
      (error: unknown) => {
        figure.textContent = '';
        answer('error', error instanceof Refusal ? error.code : 'unreadable');
      },
    );
  });
}
