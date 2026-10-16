// SPDX-License-Identifier: MIT
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import { licenseNotices } from './server/license-notices.mjs';

// The figure frame: a page of its own, served only by the preview origin under /figures/
// (docs/specs/figures.md, FG-14). It is built after the GUI, into the GUI's dist/figures.
// Its third-party code (Vega, Vega-Lite and their d3 dependencies) ships with the notices
// the license manifest lists for it.
export default defineConfig({
  root: fileURLToPath(new URL('./src/figures', import.meta.url)),
  base: '/figures/',
  publicDir: false,
  plugins: [licenseNotices('figures', fileURLToPath(new URL('.', import.meta.url)))],
  build: {
    outDir: fileURLToPath(new URL('./dist/figures', import.meta.url)),
    emptyOutDir: true,
    // Every asset is a file of its own: the preview origin's CSP admits no data: scripts.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    sourcemap: false,
    target: 'es2023',
  },
});
