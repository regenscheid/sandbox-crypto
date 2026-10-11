// SPDX-License-Identifier: MIT
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

// The server is sandbox-science-workspace; `npm run dev` passes its routes through.
const server = 'http://127.0.0.1:8200';

export default defineConfig({
  plugins: [svelte()],
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
    include: ['src/**/*.test.ts'],
    environment: 'node',
  },
});
