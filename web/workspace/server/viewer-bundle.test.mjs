// SPDX-License-Identifier: MIT
// The PDF viewer's bundle (docs/specs/pdf-viewer.md, PV-5): its own code and PDF.js, none
// of the workspace's client, and exactly the PDF.js files the license manifest lists.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test } from 'vitest';
import { build } from 'vite';
import { collectNotices } from './license-notices.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const manifest = JSON.parse(readFileSync(resolve(root, 'licenses/manifest.json'), 'utf8'));
const pinned = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8')).dependencies;
const PDFJS = 'node_modules/pdfjs-dist/';
const EXCLUDED = /LiberationSans|quickjs|pdf\.sandbox|\.wasm$|\.icc$/;

async function viewerBuild() {
  const result = await build({
    logLevel: 'silent',
    configFile: resolve(root, 'vite.viewer.config.ts'),
    build: { write: false },
  });
  return Array.isArray(result) ? result.flatMap((r) => r.output) : result.output;
}

test('the viewer bundle holds its own code and PDF.js, and no workspace client', async () => {
  const output = await viewerBuild();
  const ids = output
    .filter((item) => item.type === 'chunk')
    .flatMap((chunk) => chunk.moduleIds)
    .map((id) => id.replaceAll('\\', '/'));
  const project = ids.filter((id) => id.startsWith(root.replaceAll('\\', '/')) && !id.includes('/node_modules/'));
  expect(project.length).toBeGreaterThan(0);
  for (const id of project) expect(id).toMatch(/\/src\/viewer\/[a-z]+\.(ts|css|html)$/);
  expect(ids.some((id) => id.includes('/node_modules/pdfjs-dist/build/pdf.mjs'))).toBe(true);
  for (const chunk of output.filter((item) => item.type === 'chunk')) {
    expect(chunk.code).not.toContain('/api/v0');
    expect(chunk.code).not.toContain('sandbox-science-session');
  }
}, 60000);

test('the viewer ships exactly the PDF.js files the license manifest lists, of a pinned version', async () => {
  const output = await viewerBuild();
  const names = output.map((item) => item.fileName);
  expect(names.filter((name) => EXCLUDED.test(name))).toEqual([]);
  const shipped = names
    .filter((name) => /^(cmaps|standard_fonts|wasm)\//.test(name))
    .concat(names.some((name) => /^assets\/pdf\.worker\.min-[\w-]+\.mjs$/.test(name)) ? ['build/pdf.worker.min.mjs'] : []);
  expect(shipped.sort()).toEqual(Object.keys(manifest.pdfjsAssets).sort());
  expect(pinned['pdfjs-dist']).toMatch(/^\d+\.\d+\.\d+$/);
  const report = JSON.parse(output.find((item) => item.fileName === 'license-manifest.json').source);
  expect(report.packages.find((item) => item.name === 'pdfjs-dist')).toMatchObject({
    version: pinned['pdfjs-dist'],
    license: 'Apache-2.0',
  });
  const notices = output.find((item) => item.fileName === 'THIRD-PARTY-NOTICES.txt').source;
  for (const file of ['LICENSE', 'cmaps/LICENSE', 'standard_fonts/LICENSE_FOXIT', 'wasm/LICENSE_OPENJPEG'])
    expect(notices).toContain(`===== pdfjs-dist@${pinned['pdfjs-dist']} — ${file} =====`);
  expect(notices).not.toContain('LICENSE_LIBERATION');
}, 60000);

/** A bundle as the viewer's build makes it, with these PDF.js files copied in. */
function bundleWith(files) {
  const bundle = {
    'assets/index.js': {
      type: 'chunk', fileName: 'assets/index.js', code: 'export {};',
      moduleIds: [resolve(root, 'src/viewer/main.ts'), resolve(root, PDFJS, 'build/pdf.mjs')],
      imports: [], dynamicImports: [],
    },
  };
  for (const name of files) {
    bundle[name] = { type: 'asset', fileName: name, source: readFileSync(resolve(root, PDFJS, name)),
      originalFileNames: [resolve(root, PDFJS, name)] };
  }
  return bundle;
}

test('the notices refuse an excluded PDF.js file, an unlisted one, or a listed one missing', () => {
  const listed = Object.keys(manifest.pdfjsAssets);
  expect(() => collectNotices(root, bundleWith(listed), 'viewer')).not.toThrow();
  for (const excluded of ['standard_fonts/LiberationSans-Regular.ttf', 'wasm/quickjs-eval.js',
    'wasm/openjpeg.wasm', 'build/pdf.sandbox.min.mjs', 'iccs/CGATS001Compat-v2-micro.icc']) {
    expect(() => collectNotices(root, bundleWith([...listed, excluded]), 'viewer'))
      .toThrow(`Uncovered PDF.js asset: ${excluded}`);
  }
  expect(() => collectNotices(root, bundleWith(listed.slice(1)), 'viewer'))
    .toThrow('The PDF.js files shipped differ');
  expect(() => collectNotices(root, bundleWith(listed), 'gui'))
    .toThrow("PDF.js belongs to the viewer's bundle only");
});
