// SPDX-License-Identifier: MIT
/**
 * The figure frame's bundle (docs/specs/figures.md, FG-14), served only by the preview
 * origin under /figures/. In the page it is the frame (page.ts); in a worker, which the
 * page starts from this same bundle for each chart, it draws the chart (chartWorker.ts),
 * so the license notices that cover the bundle cover the worker too (review of #500, M1).
 */
import { serveCharts } from './chartWorker';
import { startPage } from './page';
import './figures.css';

if (typeof document === 'undefined') serveCharts();
else startPage(import.meta.url);
