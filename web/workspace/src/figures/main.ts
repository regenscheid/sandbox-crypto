// SPDX-License-Identifier: MIT
/**
 * The figure frame's page (docs/specs/figures.md, FG-14), served only by the preview
 * origin under /figures/. It takes one request from the workspace's window, fetches only
 * that figure's capabilities from its own origin, draws it, and answers with its size and
 * a status. An SVG file is drawn as an image (`<img>` of a blob), so it runs no script
 * and loads nothing, whatever it holds.
 */
import {
  accepted,
  frameWorkspace,
  MAX_FILE_BYTES,
  statusMessage,
  type FigureRequest,
} from './protocol';
import { framedBy } from '../viewer/anchor';
import './figures.css';

const figure = document.getElementById('figure') as HTMLElement;
const workspace = frameWorkspace(location.hash);
let taken = false;

function answer(status: 'rendered' | 'error', reason = ''): void {
  if (workspace === null) return;
  const box = figure.getBoundingClientRect();
  const height = Math.max(box.height, document.documentElement.scrollHeight);
  window.parent.postMessage(statusMessage(status, box.width, height, reason), workspace);
}

async function file(capability: string, type: string): Promise<Blob> {
  const response = await fetch(`/file/${capability}`, {
    credentials: 'omit',
    cache: 'no-store',
    redirect: 'error',
  });
  if (!response.ok) throw new Error(`the file was refused (${response.status})`);
  if (!(response.headers.get('Content-Type') ?? '').startsWith(type))
    throw new Error('the file is not the type it should be');
  const data = await response.blob();
  if (data.size > MAX_FILE_BYTES) throw new Error('the file is larger than a figure draws');
  return new Blob([data], { type });
}

async function draw(request: FigureRequest): Promise<void> {
  // An SVG is shown as an image: the browser's image mode runs no script and fetches nothing.
  const blob = await file(request.capabilities[0]!, 'image/svg+xml');
  const image = new Image();
  image.alt = '';
  image.src = URL.createObjectURL(blob);
  await image.decode().catch(() => {
    throw new Error('the SVG could not be drawn');
  });
  figure.replaceChildren(image);
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
        answer('error', error instanceof Error ? error.message : 'the figure could not be drawn');
      },
    );
  });
}
