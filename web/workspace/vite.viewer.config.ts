// SPDX-License-Identifier: MIT
import { defineConfig } from 'vite';
import { licenseNotices, pdfjsAssets, VIEWER } from './server/license-notices.mjs';

// The PDF viewer: a page of its own, served only by the preview origin under /viewer/
// (docs/specs/pdf-viewer.md). It is built after the GUI, into the GUI's dist/viewer.
export default defineConfig({
  root: VIEWER.root,
  base: '/viewer/',
  publicDir: false,
  plugins: [pdfjsAssets(VIEWER.gui), licenseNotices('viewer', VIEWER.gui)],
  build: {
    outDir: VIEWER.outDir,
    emptyOutDir: true,
    // Every asset is a file of its own: the preview origin's CSP admits no data: scripts.
    assetsInlineLimit: 0,
    sourcemap: false,
    target: 'es2023',
  },
});
