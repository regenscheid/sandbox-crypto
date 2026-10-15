// SPDX-License-Identifier: MIT
/**
 * The PDF viewer's page (docs/specs/pdf-viewer.md, PV-5, PV-6): one page at a time on a
 * canvas, with PDF.js's text layer for selection; pages by number and by printed label;
 * zoom; and "Copy anchor", which posts the selection's anchor to the workspace's window
 * only. PDF.js runs through its core API: it has no eval path, and its scripting lives in
 * viewer components and a sandbox the bundle leaves out, so nothing in the PDF runs. Its
 * image decoders are the plain JavaScript ones, so the CSP needs no WebAssembly.
 */
import {
  getDocument,
  GlobalWorkerOptions,
  TextLayer,
  type PDFDocumentProxy,
  type PageViewport,
} from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import {
  anchorMessage,
  canvasRatio,
  capabilitySha,
  framedBy,
  pageByLabel,
  pageLabel,
  pdfBox,
  viewerParams,
} from './anchor';
import './viewer.css';

GlobalWorkerOptions.workerSrc = workerUrl;

const element = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const status = element<HTMLParagraphElement>('status');
const pages = element<HTMLElement>('pages');
const pageInput = element<HTMLInputElement>('page');
const labelInput = element<HTMLInputElement>('label');
const count = element<HTMLSpanElement>('count');
const zoom = element<HTMLSpanElement>('zoom');
const controls = ['previous', 'next', 'smaller', 'larger'].map((id) =>
  element<HTMLButtonElement>(id),
);
const [previous, next, smaller, larger] = controls as [
  HTMLButtonElement,
  HTMLButtonElement,
  HTMLButtonElement,
  HTMLButtonElement,
];
const anchorButton = element<HTMLButtonElement>('anchor');

const SCALES = [0.5, 0.75, 1, 1.25, 1.5, 2, 3] as const;
let scaleIndex = 2;
let pdf: PDFDocumentProxy | null = null;
let labels: string[] | null = null;
let current = 1;
let shown: { container: HTMLElement; viewport: PageViewport } | null = null;
let rendering = 0;

async function show(index: number): Promise<void> {
  if (!pdf) return;
  current = Math.min(Math.max(1, index), pdf.numPages);
  const generation = ++rendering;
  const page = await pdf.getPage(current);
  if (generation !== rendering) return;
  const scale = SCALES[scaleIndex]!;
  const viewport = page.getViewport({ scale });
  const container = document.createElement('div');
  container.className = 'page';
  container.style.setProperty('--total-scale-factor', String(scale));
  container.style.width = `${viewport.width}px`;
  container.style.height = `${viewport.height}px`;
  const canvas = document.createElement('canvas');
  // A page drawn larger than the canvas cap is drawn at a lower resolution, same size.
  const ratio = canvasRatio(viewport.width, viewport.height, window.devicePixelRatio || 1);
  canvas.width = Math.floor(viewport.width * ratio);
  canvas.height = Math.floor(viewport.height * ratio);
  canvas.style.width = `${viewport.width}px`;
  canvas.style.height = `${viewport.height}px`;
  const text = document.createElement('div');
  text.className = 'textLayer';
  container.append(canvas, text);
  await page.render({
    canvas,
    viewport,
    transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
  }).promise;
  if (generation !== rendering) return;
  await new TextLayer({ textContentSource: page.streamTextContent(), container: text, viewport })
    .render()
    .catch(() => undefined); // a page without text still shows
  if (generation !== rendering) return;
  pages.replaceChildren(container);
  shown = { container, viewport };
  pageInput.value = String(current);
  labelInput.value = pageLabel(labels, current);
  count.textContent = `of ${pdf.numPages}`;
  zoom.textContent = `${Math.round(scale * 100)}%`;
  previous.disabled = current <= 1;
  next.disabled = current >= pdf.numPages;
  smaller.disabled = scaleIndex === 0;
  larger.disabled = scaleIndex === SCALES.length - 1;
  status.textContent = '';
}

/** Show a page, saying so when it cannot be drawn rather than failing silently. */
function go(index: number): void {
  show(index).catch(() => {
    status.textContent = `Page ${index} could not be drawn.`;
  });
}

/** Post the anchor of the selection on the page shown; say why when there is none. */
function copyAnchor(sha256: string, workspace: string): void {
  const selection = document.getSelection();
  if (!shown || !selection || selection.isCollapsed || !selection.rangeCount) {
    status.textContent = 'Select text on the page first.';
    return;
  }
  const range = selection.getRangeAt(0);
  if (!shown.container.contains(range.commonAncestorContainer)) {
    status.textContent = 'Select text on the page shown.';
    return;
  }
  const origin = shown.container.getBoundingClientRect();
  const rects = [...range.getClientRects()].map((rect) => ({
    left: rect.left - origin.left,
    top: rect.top - origin.top,
    right: rect.right - origin.left,
    bottom: rect.bottom - origin.top,
  }));
  const viewport = shown.viewport;
  const box = pdfBox(rects, (x, y) => viewport.convertToPdfPoint(x, y) as number[]);
  if (!box) {
    status.textContent = 'The selection covers nothing on the page.';
    return;
  }
  const message = anchorMessage(sha256, current, pageLabel(labels, current), box, `${selection}`);
  // Only the workspace's window may receive it; a page that framed the viewer elsewhere
  // gets nothing (and the preview origin's CSP lets only the workspace frame it).
  window.parent.postMessage(message, workspace);
  status.textContent = `Anchor sent: page ${current} (${message.anchor.page.label}).`;
}

async function open(): Promise<void> {
  const params = viewerParams(location.hash);
  const sha256 = params && capabilitySha(params.capability);
  if (!params || !sha256) {
    status.textContent = 'This viewer opens a PDF only from a link the workspace made.';
    return;
  }
  // Where the browser says who frames the viewer, it must be the workspace the link names.
  if (!framedBy(params.workspace, location.ancestorOrigins)) {
    status.textContent = 'This viewer opens only inside the workspace that made its link.';
    return;
  }
  try {
    pdf = await getDocument({
      url: `/pdf/${params.capability}`,
      enableXfa: false,
      useWasm: false,
      useSystemFonts: true,
      cMapUrl: '/viewer/cmaps/',
      cMapPacked: true,
      standardFontDataUrl: '/viewer/standard_fonts/',
      wasmUrl: '/viewer/wasm/',
    }).promise;
  } catch {
    status.textContent = 'The PDF could not be opened: the link may have expired. Open it again.';
    return;
  }
  labels = await pdf.getPageLabels().catch(() => null);
  for (const control of [pageInput, labelInput]) control.disabled = false;
  anchorButton.disabled = false;
  previous.addEventListener('click', () => go(current - 1));
  next.addEventListener('click', () => go(current + 1));
  smaller.addEventListener('click', () => {
    scaleIndex = Math.max(0, scaleIndex - 1);
    go(current);
  });
  larger.addEventListener('click', () => {
    scaleIndex = Math.min(SCALES.length - 1, scaleIndex + 1);
    go(current);
  });
  pageInput.addEventListener('change', () => go(Number(pageInput.value) || 1));
  labelInput.addEventListener('change', () => {
    const found = pageByLabel(labels, labelInput.value, pdf?.numPages ?? 0);
    if (found) go(found);
    else status.textContent = `No page is labelled ${labelInput.value}.`;
  });
  anchorButton.addEventListener('click', () => copyAnchor(sha256, params.workspace));
  // A page past the last opens the last (show).
  go(params.page ?? 1);
}

// The workspace opens a PDF again with a fresh capability by changing only the fragment,
// which does not reload a page by itself.
window.addEventListener('hashchange', () => location.reload());
void open();
