// SPDX-License-Identifier: MIT
/**
 * The figure frame's chart worker (docs/specs/figures.md, FG-15, FG-17; review of #500,
 * M1): compiling, running and drawing a chart happen here, off the frame's thread, which
 * may be the workspace page's own, so the frame can end the work at the time limit by
 * terminating this worker. It takes one chart, reads its data from its capabilities on
 * this origin, and answers with its SVG and name, or a refusal's code. The frame sanitizes
 * the SVG before it shows it.
 */
import { Refusal } from './chart';
import { chartSvg } from './vegaChart';

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

/** Serve one chart at a time to the page that started this worker. */
export function serveCharts(): void {
  self.onmessage = async (event: MessageEvent) => {
    const { source, paths, capabilities } = event.data as {
      source: string;
      paths: string[];
      capabilities: string[];
    };
    const files = new Map(paths.map((path, index) => [path, capabilities[index]!]));
    try {
      self.postMessage({ ok: true, ...(await chartSvg(source, files, dataText)) });
    } catch (error) {
      self.postMessage({ ok: false, code: error instanceof Refusal ? error.code : 'invalid' });
    }
  };
}
