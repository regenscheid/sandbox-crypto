// SPDX-License-Identifier: MIT
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, realpathSync, readdirSync } from 'node:fs';
import { isAbsolute, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const virtualPackages = new Map([
  ['\0rolldown/runtime.js', 'rolldown'],
  ['\0vite/modulepreload-polyfill.js', 'vite'],
  ['\0vite/preload-helper.js', 'vite'],
]);

// The PDF.js files the viewer ships beside its code (docs/specs/pdf-viewer.md): the packed
// character maps, the Foxit standard fonts, the plain JavaScript image decoders and the
// worker. Never the GPL-licensed Liberation fonts, the QuickJS scripting engine, PDF.js's
// scripting sandbox, or any WebAssembly, which the viewer's CSP would not run.
const PDFJS = 'node_modules/pdfjs-dist/';
const PDFJS_SHIPPED = [/^cmaps\/[^/]+\.bcmap$/, /^standard_fonts\/Foxit[^/]+\.pfb$/,
  /^wasm\/[a-z0-9]+_nowasm_fallback\.js$/, /^build\/pdf\.worker\.min\.mjs$/];
const PDFJS_EXCLUDED = /LiberationSans|quickjs|pdf\.sandbox|pdf_viewer|^legacy\/|\.wasm$/;

// Where the viewer's build reads and writes: its page below the GUI's sources, its output
// in the GUI's dist/viewer, which the preview origin serves under /viewer/.
const gui = fileURLToPath(new URL('..', import.meta.url));
export const VIEWER = { gui, root: resolve(gui, 'src/viewer'), outDir: resolve(gui, 'dist/viewer') };

/** @param {string | Uint8Array} bytes */
function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

/** @param {string} root @param {string} path */
function inside(root, path) {
  const name = relative(root, path);
  if (name === '..' || name.startsWith(`..${sep}`) || isAbsolute(name)) {
    throw new Error(`License source escapes the GUI root: ${path}`);
  }
  return name.split(sep).join('/');
}

/** @param {string} path @param {string} expected */
function checkedBytes(path, expected) {
  const bytes = readFileSync(path);
  if (sha256(bytes) !== expected) throw new Error(`License input digest mismatch: ${path}`);
  return bytes;
}

/** Extract exact copyright/license statements from the checked distributed TTF. @param {Buffer} bytes */
function fontStatements(bytes) {
  const count = bytes.readUInt16BE(4);
  for (let i = 0; i < count; i++) {
    const record = 12 + i * 16;
    if (bytes.toString('ascii', record, record + 4) !== 'name') continue;
    const offset = bytes.readUInt32BE(record + 8);
    const strings = offset + bytes.readUInt16BE(offset + 4);
    const statements = new Set();
    for (let n = 0; n < bytes.readUInt16BE(offset + 2); n++) {
      const entry = offset + 6 + n * 12;
      const platform = bytes.readUInt16BE(entry);
      const name = bytes.readUInt16BE(entry + 6);
      if (platform !== 3 || ![0, 13, 14].includes(name)) continue;
      const start = strings + bytes.readUInt16BE(entry + 10);
      const end = start + bytes.readUInt16BE(entry + 8);
      statements.add(Buffer.from(bytes.subarray(start, end)).swap16().toString('utf16le'));
    }
    if (![...statements].some(text => text.includes('SIL Open Font License, Version 1.1'))) break;
    return [...statements].sort().join('\n\n');
  }
  throw new Error('Checked KaTeX font lacks its expected OFL attribution');
}

/**
 * Build notices from actual emitted module/asset origins; unknown origins require review.
 * @param {string} root
 * @param {import('rolldown').OutputBundle} bundle
 * @param {'gui' | 'math' | 'viewer' | 'figures' | 'figure-check'} distribution
 * @param {string} sourceRoot what the bundle's relative names are relative to
 */
export function collectNotices(root, bundle, distribution = 'gui', sourceRoot = root) {
  if (!['gui', 'math', 'viewer', 'figures', 'figure-check'].includes(distribution)) {
    throw new Error('Unknown notice distribution');
  }
  root = realpathSync(root);
  const manifest = JSON.parse(readFileSync(resolve(root, 'licenses/manifest.json'), 'utf8'));
  const lock = JSON.parse(readFileSync(resolve(root, 'package-lock.json'), 'utf8'));
  const packages = new Set();
  const fontPaths = new Set();
  let katexRuntime = false;
  let sveltePromise = false;
  const pdfjsFiles = new Set();

  /** @param {string} id */
  function origin(id) {
    if (virtualPackages.has(id)) {
      const name = virtualPackages.get(id);
      packages.add(name);
      return name;
    }
    if (id.includes('\0')) throw new Error(`Uncovered virtual license origin: ${id}`);
    const name = inside(root, realpathSync(resolve(sourceRoot, id.split('?')[0])));
    if (name.startsWith(PDFJS)) {
      if (distribution !== 'viewer') throw new Error(`PDF.js belongs to the viewer's bundle only: ${name}`);
      // Bundled code is PDF.js's core API, byte for byte, and the worker's URL (the worker
      // itself is a copied asset): never its scripting sandbox, viewer components or
      // legacy build.
      const file = name.slice(PDFJS.length);
      const query = id.includes('?') ? id.slice(id.indexOf('?')) : '';
      const pinned = query === '' ? manifest.pdfjsRuntime[file] : undefined;
      const workerUrl = query === '?url' && file === 'build/pdf.worker.min.mjs';
      if (PDFJS_EXCLUDED.test(file) || !(pinned || workerUrl)) {
        throw new Error(`Uncovered PDF.js module: ${file}${query}`);
      }
      if (pinned) checkedBytes(resolve(root, name), pinned);
    }
    if (name.startsWith('node_modules/')) {
      // A package another one needs at its own version sits in that one's node_modules;
      // the manifest names it by that place, such as `vega-lite/node_modules/vega-expression`.
      const match = /^node_modules\/((?:@[^/]+\/)?[^/]+)\/(?:node_modules\/((?:@[^/]+\/)?[^/]+)\/)?/.exec(name);
      const packageName = match?.[2] ? `${match[1]}/node_modules/${match[2]}` : match?.[1];
      const rest = match ? name.slice(match[0].length) : '';
      if (!packageName || !manifest.packages[packageName] || rest.includes('node_modules/')) {
        throw new Error(`Uncovered bundled package: ${name}`);
      }
      packages.add(packageName);
      if (name === `node_modules/svelte/${manifest.svelteEmbedded.source}`) sveltePromise = true;
      if (packageName === 'katex' && name !== 'node_modules/katex/dist/katex.min.css') {
        if (name !== 'node_modules/katex/dist/katex.mjs') throw new Error(`Uncovered KaTeX component: ${name}`);
        checkedBytes(resolve(root, name), manifest.katexRuntimeSha256);
        katexRuntime = true;
      }
      if (name.endsWith('.css')) {
        if (packageName !== 'katex' || name !== 'node_modules/katex/dist/katex.min.css') {
          throw new Error(`Uncovered bundled stylesheet: ${name}`);
        }
        checkedBytes(resolve(root, name), manifest.katexAssets['dist/katex.min.css']);
      }
      return packageName;
    }
    if (name !== 'index.html' && !name.startsWith('src/')) {
      throw new Error(`Uncovered project license origin: ${name}`);
    }
    return 'Sandbox Science';
  }

  // A stylesheet split out with a shared chunk has no file of its own: its origins are
  // the component styles and stylesheets of the chunks that import it.
  const styleOrigins = new Map();
  for (const item of Object.values(bundle)) {
    if (item.type !== 'chunk') continue;
    const styles = item.moduleIds.filter(id => /[?&]type=style(&|$)|\.css(\?|$)/.test(id));
    for (const css of item.viteMetadata?.importedCss ?? []) {
      styleOrigins.set(css, [...(styleOrigins.get(css) ?? []), ...styles]);
    }
  }

  const files = [];
  for (const item of Object.values(bundle).sort((a, b) => a.fileName < b.fileName ? -1 : a.fileName > b.fileName ? 1 : 0)) {
    const owners = new Set();
    if (item.type === 'chunk') {
      if (!item.moduleIds.length) throw new Error(`Chunk has no license origins: ${item.fileName}`);
      for (const id of item.moduleIds) owners.add(origin(id));
      for (const path of [...item.imports, ...item.dynamicImports]) {
        if (!bundle[path]) throw new Error(`Uncovered external bundle import: ${path}`);
      }
    } else if (item.fileName.endsWith('.css') || item.fileName === 'index.html') {
      const sources = item.originalFileNames.length
        ? item.originalFileNames
        : styleOrigins.get(item.fileName) ?? [];
      if (!sources.length) throw new Error(`Asset has no license origins: ${item.fileName}`);
      for (const id of sources) owners.add(origin(id));
      // Vite combines the imported KaTeX stylesheet with project styles.
      if (item.fileName.endsWith('.css') && packages.has('katex')) owners.add('katex');
    } else if (item.originalFileNames.length === 1 &&
        resolve(sourceRoot, item.originalFileNames[0]).startsWith(resolve(root, PDFJS))) {
      // Copied byte for byte; the manifest lists each PDF.js file that may ship.
      const name = inside(resolve(root, PDFJS), realpathSync(resolve(sourceRoot, item.originalFileNames[0])));
      if (distribution !== 'viewer' || PDFJS_EXCLUDED.test(name) || !PDFJS_SHIPPED.some(rule => rule.test(name)) ||
          !manifest.pdfjsAssets[name]) {
        throw new Error(`Uncovered PDF.js asset: ${name}`);
      }
      const bytes = checkedBytes(resolve(root, PDFJS, name), manifest.pdfjsAssets[name]);
      if (sha256(item.source) !== sha256(bytes)) throw new Error(`Modified PDF.js asset: ${item.fileName}`);
      packages.add('pdfjs-dist');
      pdfjsFiles.add(name);
      owners.add('pdfjs-dist');
    } else {
      if (item.originalFileNames.length !== 1) throw new Error(`Uncovered copied asset: ${item.fileName}`);
      const path = inside(root, realpathSync(resolve(sourceRoot, item.originalFileNames[0])));
      const name = path.replace(/^node_modules\/katex\//, '');
      if (name === path || !name.startsWith('dist/fonts/') || !manifest.katexAssets[name]) {
        throw new Error(`Uncovered copied asset: ${item.fileName}`);
      }
      const bytes = checkedBytes(resolve(root, path), manifest.katexAssets[name]);
      if (sha256(item.source) !== sha256(bytes)) throw new Error(`Modified font asset: ${item.fileName}`);
      packages.add('katex');
      fontPaths.add(name);
      owners.add('KaTeX fonts (OFL-1.1)');
    }
    files.push({ file: item.fileName, sha256: sha256(item.type === 'chunk' ? item.code : item.source),
      notices: [...owners].sort() });
  }

  if (distribution === 'viewer') {
    const listed = Object.keys(manifest.pdfjsAssets).sort();
    if (JSON.stringify([...pdfjsFiles].sort()) !== JSON.stringify(listed)) {
      throw new Error('The PDF.js files shipped differ from the license manifest\'s list');
    }
  }
  const title = { gui: 'workspace GUI', math: 'isolated math checker', viewer: 'workspace PDF viewer',
    figures: 'workspace figure frame', 'figure-check': 'isolated figure checker' };
  const sections = [`Third-party notices for the Sandbox Science ${title[distribution]}.\n` +
    'Includes runtime helpers and complete upstream notice files; some upstream files also cover build-only code.\n'];
  const packageRecords = [];
  for (const name of [...packages].sort()) {
    const approved = manifest.packages[name];
    const installed = JSON.parse(readFileSync(resolve(root, `node_modules/${name}/package.json`), 'utf8'));
    const locked = lock.packages[`node_modules/${name}`];
    if (!approved || !locked || installed.name !== name.split('/node_modules/').at(-1) ||
        installed.version !== approved.version ||
        installed.license !== approved.license || locked.version !== approved.version || locked.license !== approved.license) {
      throw new Error(`Bundled package license/version mismatch: ${name}`);
    }
    if (!approved.notices || !Object.keys(approved.notices).length) {
      throw new Error(`Bundled package has no approved primary notice: ${name}`);
    }
    for (const [file, digest] of Object.entries(approved.notices)) {
      sections.push(`===== ${name}@${installed.version} — ${file} =====\n\n` +
        checkedBytes(resolve(root, `node_modules/${name}/${file}`), digest).toString('utf8'));
    }
    const embedded = [];
    if (name === 'katex' && katexRuntime) {
      for (const file of ['src/utils.ts', 'src/stretchy.ts']) {
        const component = manifest.katexEmbedded[file];
        const text = checkedBytes(resolve(root, `node_modules/katex/${file}`), component.sha256).toString('utf8');
        if (!text.includes(component.attribution)) throw new Error(`Missing embedded attribution: ${file}`);
        sections.push(`===== Embedded component in katex@${installed.version} — ${file} =====\n\n${component.attribution}`);
        embedded.push({ source: file, sha256: component.sha256, license: component.license });
      }
      sections.push('===== React-derived KaTeX helpers — Apache-2.0 =====\n\n' +
        checkedBytes(resolve(root, 'licenses/React-Apache-2.0.txt'), manifest.reactLicense.sha256).toString('utf8'));
    }
    if (name === 'svelte' && sveltePromise) {
      const component = manifest.svelteEmbedded;
      const text = checkedBytes(resolve(root, `node_modules/svelte/${component.source}`), component.sha256).toString('utf8');
      if (!text.includes(component.attribution)) throw new Error('Missing embedded Svelte attribution');
      sections.push(`===== Embedded component in svelte@${installed.version} — ${component.source} =====\n\n` +
        component.attribution + '\n\n' + checkedBytes(resolve(root, 'licenses/is-promise-MIT.txt'), component.upstream.sha256).toString('utf8'));
      embedded.push({ source: component.source, sha256: component.sha256, license: component.license });
    }
    packageRecords.push({ name, version: installed.version, license: installed.license,
      notices: approved.notices, embedded });
  }
  if (fontPaths.size || katexRuntime) {
    const fontNames = [...new Set([...fontPaths].map(path => path.replace(/\.(ttf|woff2?)$/, '.ttf')))].sort();
    const attributions = fontNames.map(path => fontStatements(
      checkedBytes(resolve(root, `node_modules/katex/${path}`), manifest.katexAssets[path])));
    const ofl = checkedBytes(resolve(root, 'licenses/OFL-1.1.txt'), manifest.ofl.sha256).toString('utf8');
    // The official download begins with an example copyright header, not these fonts' attribution.
    const licenseStart = ofl.indexOf('-----------------------------------------------------------');
    if (licenseStart < 0) throw new Error('Pinned OFL text lacks its license body');
    sections.push('===== KaTeX fonts and glyph-derived SVG assets — SIL OFL 1.1 =====\n\n' +
      [...new Set(attributions)].sort().join('\n\n') + '\n\n' + ofl.slice(licenseStart));
  }
  const project = checkedBytes(resolve(root, '../../LICENSE'), manifest.projectLicenseSha256).toString('utf8');
  const notices = sections.join('\n\n');
  // A checker ships with its launcher, which the manifest pins (the figure checker's
  // beside the math checker's, in dist-math/figures).
  const launcher = { math: manifest.mathLauncher, 'figure-check': manifest.figureLauncher }[distribution];
  const extra = distribution === 'math' ? {
    'LICENSE-KaTeX.txt': checkedBytes(resolve(root, 'node_modules/katex/LICENSE'),
      manifest.packages.katex.notices.LICENSE).toString('utf8'),
    'check-math.mjs': checkedBytes(resolve(root, 'server/check-math.mjs'), manifest.mathLauncher.sha256).toString('utf8'),
  } : distribution === 'figure-check' ? {
    'check-figures.mjs': checkedBytes(resolve(root, launcher.source), launcher.sha256).toString('utf8'),
  } : {};
  for (const [file, bytes] of Object.entries(extra)) {
    if (bundle[file]) throw new Error(`Notice distribution file collision: ${file}`);
    files.push({ file, sha256: sha256(bytes), notices: [file === 'LICENSE-KaTeX.txt' ? 'katex' : 'Sandbox Science'] });
  }
  files.push({ file: 'THIRD-PARTY-NOTICES.txt', sha256: sha256(notices),
    notices: [...packages, ...(fontPaths.size ? ['KaTeX fonts (OFL-1.1)'] : [])].sort() });
  files.push({ file: 'LICENSE-Sandbox-Science.txt', sha256: sha256(project), notices: ['Sandbox Science'] });
  files.sort((a, b) => a.file < b.file ? -1 : a.file > b.file ? 1 : 0);
  return {
    ...extra,
    'THIRD-PARTY-NOTICES.txt': notices,
    'LICENSE-Sandbox-Science.txt': project,
    'license-manifest.json': JSON.stringify({ protocol: 1,
      ...(launcher ? { distribution: distribution === 'math' ? 'math-checker' : 'figure-checker',
        externalRuntime: { name: 'Node.js', providedBy: 'operator', builtins: launcher.builtins } } : {}),
      packages: packageRecords,
      fontAndSvgLicense: fontPaths.size || katexRuntime ? manifest.ofl : null,
      reactLicense: katexRuntime ? manifest.reactLicense : null,
      isPromiseLicense: sveltePromise ? manifest.svelteEmbedded.upstream : null, files }, null, 2) + '\n',
  };
}

/**
 * Include checked runtime and asset notices.
 * @param {'gui' | 'math' | 'viewer' | 'figures' | 'figure-check'} distribution
 * @param {string} [gui] the GUI's directory, when the build's root is below it
 * @returns {import('vite').Plugin}
 */
export function licenseNotices(distribution = 'gui', gui) {
  let root;
  let sourceRoot;
  return {
    name: 'workspace-license-notices', apply: 'build', enforce: 'post',
    configResolved(config) {
      sourceRoot = config.root;
      root = gui ?? config.root;
      if (config.publicDir && existsSync(config.publicDir) && readdirSync(config.publicDir).length) {
        throw new Error('Public assets need explicit license coverage before copying');
      }
    },
    generateBundle: { order: 'post', handler(_, bundle) {
      for (const [fileName, source] of Object.entries(collectNotices(root, bundle, distribution, sourceRoot))) {
        this.emitFile({ type: 'asset', fileName, source });
      }
    } },
  };
}

/** Verify the complete standalone math distribution after Vite writes it; the figure
 * checker's, in its `figures` directory, is verified on its own. @param {string} root */
export function verifyMathDistribution(root) {
  verifyChecker(root, 'dist-math', 'math-checker', 'check-math.mjs', ['LICENSE-KaTeX.txt', 'mathcheck.mjs']);
}

/** Verify the figure checker's distribution, beside the math checker's. @param {string} root */
export function verifyFigureDistribution(root) {
  verifyChecker(root, 'dist-math/figures', 'figure-checker', 'check-figures.mjs', ['figurecheck.mjs']);
}

/** @param {string} root @param {string} at @param {string} name @param {string} launcher
 * @param {string[]} own */
function verifyChecker(root, at, name, launcher, own) {
  const directory = resolve(root, at);
  const expected = ['LICENSE-Sandbox-Science.txt', 'THIRD-PARTY-NOTICES.txt', launcher,
    'license-manifest.json', ...own].sort();
  // The figure checker's own directory sits beside the math checker's files.
  const actual = readdirSync(directory, { withFileTypes: true })
    .filter(item => !(at === 'dist-math' && item.name === 'figures' && item.isDirectory()))
    .map(item => item.name).sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error(`Uncovered ${name} distribution files`);
  const report = JSON.parse(readFileSync(resolve(directory, 'license-manifest.json'), 'utf8'));
  if (report.protocol !== 1 || report.distribution !== name ||
      JSON.stringify(report.files.map(file => file.file).sort()) !==
        JSON.stringify(expected.filter(file => file !== 'license-manifest.json'))) {
    throw new Error(`Incomplete ${name} distribution manifest`);
  }
  for (const file of report.files) checkedBytes(resolve(directory, file.file), file.sha256);
  if (!readFileSync(resolve(directory, launcher)).equals(readFileSync(resolve(root, 'server', launcher)))) {
    throw new Error(`The ${name}'s launcher differs from its project source`);
  }
}

/**
 * Copy the PDF.js files the viewer loads at run time, under the names PDF.js asks for;
 * the worker comes in through its `?url` import. @param {string} gui the GUI's directory
 * @returns {import('vite').Plugin}
 */
export function pdfjsAssets(gui) {
  return {
    name: 'workspace-pdfjs-assets', apply: 'build',
    generateBundle() {
      for (const directory of ['cmaps', 'standard_fonts', 'wasm']) {
        for (const file of readdirSync(resolve(gui, PDFJS, directory)).sort()) {
          const name = `${directory}/${file}`;
          if (!PDFJS_SHIPPED.some(rule => rule.test(name)) || PDFJS_EXCLUDED.test(name)) continue;
          const path = resolve(gui, PDFJS, name);
          this.emitFile({ type: 'asset', fileName: name, originalFileName: path, source: readFileSync(path) });
        }
      }
    },
  };
}
