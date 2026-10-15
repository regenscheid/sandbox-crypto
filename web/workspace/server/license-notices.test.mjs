// SPDX-License-Identifier: MIT
import { createHash } from 'node:crypto';
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, expect, test } from 'vitest';
import { build } from 'vite';
import { collectNotices, licenseNotices } from './license-notices.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const manifest = JSON.parse(readFileSync(resolve(root, 'licenses/manifest.json'), 'utf8'));
const temporary = [];
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
afterEach(() => { for (const path of temporary.splice(0)) rmSync(path, { recursive: true, force: true }); });

function fixture() {
  const temp = mkdtempSync(resolve(tmpdir(), 'workspace-notices-'));
  temporary.push(temp);
  const app = resolve(temp, 'web/workspace');
  mkdirSync(app, { recursive: true });
  const copy = (from, to = from) => {
    mkdirSync(dirname(resolve(app, to)), { recursive: true });
    cpSync(resolve(root, from), resolve(app, to));
  };
  cpSync(resolve(root, 'licenses'), resolve(app, 'licenses'), { recursive: true });
  copy('package-lock.json');
  copy('../../LICENSE');
  for (const [name, packageInfo] of Object.entries(manifest.packages)) {
    for (const file of ['package.json', ...Object.keys(packageInfo.notices)]) copy(`node_modules/${name}/${file}`);
  }
  for (const file of ['dist/katex.mjs', 'src/utils.ts', 'src/stretchy.ts', 'dist/katex.min.css',
    'dist/fonts/KaTeX_AMS-Regular.woff2', 'dist/fonts/KaTeX_AMS-Regular.ttf']) {
    copy(`node_modules/katex/${file}`);
  }
  for (const file of ['src/main.ts', 'src/app.css', 'index.html', 'node_modules/markdown-it/dist/markdown-it.mjs']) copy(file);
  copy('node_modules/svelte/src/internal/shared/utils.js');
  const bundle = {
    'assets/index.js': { type: 'chunk', fileName: 'assets/index.js', code: 'export {};',
      moduleIds: [resolve(app, 'src/main.ts'), resolve(app, 'node_modules/markdown-it/dist/markdown-it.mjs'),
        resolve(app, 'node_modules/katex/dist/katex.min.css'), resolve(app, 'node_modules/katex/dist/katex.mjs'),
        resolve(app, 'node_modules/svelte/src/internal/shared/utils.js'),
        '\0vite/modulepreload-polyfill.js', '\0rolldown/runtime.js'],
      imports: [], dynamicImports: [] },
    'assets/index.css': { type: 'asset', fileName: 'assets/index.css', source: 'body {}', originalFileNames: ['src/app.css'] },
    'assets/font.woff2': { type: 'asset', fileName: 'assets/font.woff2',
      source: readFileSync(resolve(app, 'node_modules/katex/dist/fonts/KaTeX_AMS-Regular.woff2')),
      originalFileNames: ['node_modules/katex/dist/fonts/KaTeX_AMS-Regular.woff2'] },
    'index.html': { type: 'asset', fileName: 'index.html', source: '<html></html>', originalFileNames: ['index.html'] },
  };
  return { app, bundle, collect: () => collectNotices(app, bundle) };
}

test('real production builds ship every runtime notice and exact copied font with deterministic coverage', async () => {
  const options = { root, logLevel: 'silent', build: { write: false } };
  const first = (await build(options)).output;
  const second = (await build(options)).output;
  const contents = output => new Map(output.map(item => [item.fileName, item.type === 'chunk' ? item.code : item.source]));
  const a = contents(first), b = contents(second);
  expect([...a.keys()].sort()).toEqual([...b.keys()].sort());
  for (const [name, bytes] of a) expect(hash(bytes)).toBe(hash(b.get(name)));
  const notice = a.get('THIRD-PARTY-NOTICES.txt');
  const report = JSON.parse(a.get('license-manifest.json'));
  expect(report.packages.map(p => p.name)).toEqual([
    'entities', 'katex', 'linkify-it', 'markdown-it', 'mdurl', 'punycode.js', 'rolldown', 'svelte', 'uc.micro', 'vite',
  ]);
  for (const packageInfo of report.packages) {
    for (const file of Object.keys(packageInfo.notices)) {
      expect(notice).toContain(readFileSync(resolve(root, `node_modules/${packageInfo.name}/${file}`), 'utf8'));
    }
  }
  expect(a.get('LICENSE-Sandbox-Science.txt')).toBe(readFileSync(resolve(root, '../../LICENSE'), 'utf8'));
  expect(notice).toContain('Copyright (c) 2009-2010 Design Science, Inc.');
  expect(notice).toContain('SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007');
  expect(notice).toContain("// hyphenate and escape adapted from Facebook's React under Apache 2 license");
  expect(notice).toContain('// Many of the KaTeX SVG images have been adapted from glyphs in KaTeX fonts.');
  expect(notice).toContain(readFileSync(resolve(root, 'licenses/React-Apache-2.0.txt'), 'utf8'));
  expect(notice).toContain('// Adapted from https://github.com/then/is-promise/blob/master/index.js');
  expect(notice).toContain(readFileSync(resolve(root, 'licenses/is-promise-MIT.txt'), 'utf8'));
  expect(report.packages.find(p => p.name === 'katex').embedded.map(e => e.license)).toEqual(['Apache-2.0', 'OFL-1.1']);
  expect(notice).not.toContain('<Copyright Holder>');
  expect(notice.match(/with Reserved Font Name KaTeX_/g)).toHaveLength(12);
  expect(report.files.filter(f => f.notices.includes('KaTeX fonts (OFL-1.1)') && /\.(ttf|woff2?)$/.test(f.file))).toHaveLength(60);
  expect(report.files.map(f => f.file).sort()).toEqual([...a.keys()].filter(f => f !== 'license-manifest.json').sort());
  for (const file of report.files) expect(hash(a.get(file.file))).toBe(file.sha256);
}, 10000);

test('notice output is independent of bundle traversal order', () => {
  const { app, bundle } = fixture();
  expect(collectNotices(app, Object.fromEntries(Object.entries(bundle).reverse()))).toEqual(collectNotices(app, bundle));
});

test('KaTeX code alone retains React and glyph-derived SVG terms without any font or CSS asset', () => {
  const { bundle, collect } = fixture();
  delete bundle['assets/font.woff2'];
  delete bundle['assets/index.css'];
  bundle['assets/index.js'].moduleIds = bundle['assets/index.js'].moduleIds.filter(id => !id.endsWith('.css'));
  const output = collect();
  expect(output['THIRD-PARTY-NOTICES.txt']).toContain('// Copyright (c) 2014-2017 Khan Academy (<www.khanacademy.org>)');
  expect(output['THIRD-PARTY-NOTICES.txt']).toContain('SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007');
  expect(output['THIRD-PARTY-NOTICES.txt']).toContain("Facebook's React under Apache 2 license");
  const report = JSON.parse(output['license-manifest.json']);
  expect(report.fontAndSvgLicense.sha256).toBe(manifest.ofl.sha256);
  expect(report.files.some(f => /\.(ttf|woff2?)$/.test(f.file))).toBe(false);
});

test.each(['node_modules/markdown-it/LICENSE', 'node_modules/rolldown/THIRD-PARTY-LICENSE',
  'licenses/OFL-1.1.txt', 'licenses/React-Apache-2.0.txt', 'licenses/is-promise-MIT.txt'])(
  'refuses a missing required primary notice: %s', file => {
    const { app, collect } = fixture();
    rmSync(resolve(app, file));
    expect(collect).toThrow();
  });

test.each(['node_modules/markdown-it/LICENSE', 'licenses/OFL-1.1.txt', 'licenses/React-Apache-2.0.txt',
  'node_modules/katex/src/utils.ts', 'node_modules/katex/src/stretchy.ts', 'node_modules/katex/dist/katex.mjs',
  'node_modules/svelte/src/internal/shared/utils.js', 'licenses/is-promise-MIT.txt', '../../LICENSE'])(
  'refuses changed license text: %s', file => {
    const { app, collect } = fixture();
    writeFileSync(resolve(app, file), 'a different notice');
    expect(collect).toThrow('digest mismatch');
  });

test.each(['version', 'license'])("refuses installed package %s differing from its approved lock", field => {
  const { app, collect } = fixture();
  const path = resolve(app, 'node_modules/markdown-it/package.json');
  const data = JSON.parse(readFileSync(path, 'utf8'));
  data[field] = 'unapproved';
  writeFileSync(path, JSON.stringify(data));
  expect(collect).toThrow('license/version mismatch');
});

test('refuses a changed lock rather than accepting the still-installed package', () => {
  const { app, collect } = fixture();
  const path = resolve(app, 'package-lock.json');
  const data = JSON.parse(readFileSync(path, 'utf8'));
  data.packages['node_modules/markdown-it'].version = 'unapproved';
  writeFileSync(path, JSON.stringify(data));
  expect(collect).toThrow('license/version mismatch');
});

test('refuses an approved package whose notice list was accidentally emptied', () => {
  const { app, collect } = fixture();
  const path = resolve(app, 'licenses/manifest.json');
  const data = JSON.parse(readFileSync(path, 'utf8'));
  data.packages['markdown-it'].notices = {};
  writeFileSync(path, JSON.stringify(data));
  expect(collect).toThrow('no approved primary notice');
});

test('refuses a newly imported KaTeX embedded component until its notices are reviewed', () => {
  const { app, bundle, collect } = fixture();
  const path = resolve(app, 'node_modules/katex/contrib/new-component.js');
  mkdirSync(dirname(path));
  writeFileSync(path, 'export {};');
  bundle['assets/index.js'].moduleIds.push(path);
  expect(collect).toThrow('Uncovered KaTeX component');
});

test('refuses a newly bundled package and unclassified virtual helper', () => {
  const { app, bundle, collect } = fixture();
  mkdirSync(resolve(app, 'node_modules/new-runtime'));
  const path = resolve(app, 'node_modules/new-runtime/index.js');
  writeFileSync(path, 'export {};');
  bundle['assets/index.js'].moduleIds.push(path);
  expect(collect).toThrow('Uncovered bundled package');
  bundle['assets/index.js'].moduleIds.pop();
  bundle['assets/index.js'].moduleIds.push('\0future-helper.js');
  expect(collect).toThrow('Uncovered virtual license origin');
});

test('refuses unclassified copied assets and dependency stylesheets', () => {
  const { app, bundle, collect } = fixture();
  writeFileSync(resolve(app, 'src/image.svg'), '<svg/>');
  bundle['assets/image.svg'] = { type: 'asset', fileName: 'assets/image.svg', source: '<svg/>', originalFileNames: ['src/image.svg'] };
  expect(collect).toThrow('Uncovered copied asset');
  delete bundle['assets/image.svg'];
  writeFileSync(resolve(app, 'node_modules/markdown-it/style.css'), 'body {}');
  bundle['assets/index.js'].moduleIds.push(resolve(app, 'node_modules/markdown-it/style.css'));
  expect(collect).toThrow('Uncovered bundled stylesheet');
});

test("a stylesheet split out with a shared chunk takes its origins from the chunk's styles", () => {
  const { app, bundle, collect } = fixture();
  mkdirSync(resolve(app, 'src/views'), { recursive: true });
  writeFileSync(resolve(app, 'src/views/Banner.svelte'), '<p>x</p>');
  bundle['assets/shared.css'] = { type: 'asset', fileName: 'assets/shared.css', source: 'p {}', originalFileNames: [] };
  expect(collect).toThrow('Asset has no license origins: assets/shared.css');
  bundle['assets/shared.js'] = { type: 'chunk', fileName: 'assets/shared.js', code: 'export {};',
    moduleIds: [resolve(app, 'src/views/Banner.svelte'), resolve(app, 'src/views/Banner.svelte') + '?svelte&type=style&lang.css'],
    imports: [], dynamicImports: [], viteMetadata: { importedCss: new Set(['assets/shared.css']) } };
  expect(collect).not.toThrow();
  writeFileSync(resolve(app, 'node_modules/markdown-it/style.css'), 'body {}');
  bundle['assets/shared.js'].moduleIds.push(resolve(app, 'node_modules/markdown-it/style.css'));
  expect(collect).toThrow('Uncovered bundled stylesheet');
});

test('refuses modified emitted font bytes and changed primary font input', () => {
  const { app, bundle, collect } = fixture();
  const original = bundle['assets/font.woff2'].source;
  bundle['assets/font.woff2'].source = new Uint8Array([1, 2]);
  expect(collect).toThrow('Modified font asset');
  bundle['assets/font.woff2'].source = original;
  writeFileSync(resolve(app, 'node_modules/katex/dist/fonts/KaTeX_AMS-Regular.woff2'), 'different font');
  expect(collect).toThrow('digest mismatch');
});

test('refuses origins outside the project, external imports and originless chunks', () => {
  const { app, bundle, collect } = fixture();
  bundle['assets/index.js'].moduleIds.push(resolve(app, '../../LICENSE'));
  expect(collect).toThrow('escapes the GUI root');
  bundle['assets/index.js'].moduleIds.pop();
  bundle['assets/index.js'].imports.push('https://example.test/runtime.js');
  expect(collect).toThrow('Uncovered external bundle import');
  bundle['assets/index.js'].imports.pop();
  bundle['assets/index.js'].moduleIds = [];
  expect(collect).toThrow('no license origins');
});

test('refuses public-directory copying that bypasses the emitted asset inventory', () => {
  const { app } = fixture();
  const publicDir = resolve(app, 'public');
  mkdirSync(publicDir);
  writeFileSync(resolve(publicDir, 'logo.svg'), '<svg/>');
  expect(() => licenseNotices().configResolved({ root: app, publicDir })).toThrow('explicit license coverage');
});
