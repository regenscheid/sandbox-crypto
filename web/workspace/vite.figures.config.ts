// SPDX-License-Identifier: MIT
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

// The figure frame: a page of its own, served only by the preview origin under /figures/
// (docs/specs/figures.md, FG-14). It is built after the GUI, into the GUI's dist/figures.
// It ships no third-party code yet: Vega-Lite and Mermaid come with F5 and F6, with their
// notices.
export default defineConfig({
  root: fileURLToPath(new URL('./src/figures', import.meta.url)),
  base: '/figures/',
  publicDir: false,
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
