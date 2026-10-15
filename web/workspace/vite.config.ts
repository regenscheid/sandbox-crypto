// SPDX-License-Identifier: MIT
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';
import { licenseNotices } from './server/license-notices.mjs';

// The server is sandbox-science-workspace; `npm run dev` passes its routes through.
const server = 'http://127.0.0.1:8200';

export default defineConfig({
  plugins: [svelte(), licenseNotices()],
  build: {
    // Every asset is a file of its own: the server's Content-Security-Policy admits no
    // data: URLs, for fonts or anything else.
    assetsInlineLimit: 0,
    sourcemap: false,
  },
  server: {
    proxy: {
      '/api': { target: server, changeOrigin: true },
      '/login': { target: server, changeOrigin: true },
    },
  },
  test: {
    projects: [
      {
        extends: true,
        test: {
          name: 'node',
          include: ['src/**/*.test.ts', 'server/**/*.test.mjs'],
          exclude: ['src/**/*.dom.test.ts'],
          environment: 'node',
        },
      },
      // Views that a test mounts and clicks through, in a DOM, with Svelte's browser
      // runtime: only the files named `*.dom.test.ts`.
      {
        extends: true,
        resolve: { conditions: ['browser'] },
        test: {
          name: 'dom',
          include: ['src/**/*.dom.test.ts'],
          environment: 'happy-dom',
        },
      },
    ],
  },
});
