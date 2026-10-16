// SPDX-License-Identifier: MIT
import { defineConfig } from 'vite';
import { licenseNotices } from './server/license-notices.mjs';

// A standalone Node library beside the math checker's: the figure frame's own chart rules
// and its Vega-Lite and Vega, at the frame's versions (docs/specs/figures.md, FG-20).
export default defineConfig({
  plugins: [licenseNotices('figure-check')],
  build: {
    target: 'node22',
    outDir: 'dist-math/figures',
    lib: {
      entry: 'src/figures/figurecheck.ts',
      formats: ['es'],
      fileName: () => 'figurecheck.mjs',
    },
    sourcemap: false,
    minify: false,
    rolldownOptions: { output: { comments: { legal: true } } },
  },
});
