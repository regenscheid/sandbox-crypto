// SPDX-License-Identifier: MIT
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, realpathSync, readdirSync } from 'node:fs';
import { isAbsolute, relative, resolve, sep } from 'node:path';

const virtualPackages = new Map([
  ['\0rolldown/runtime.js', 'rolldown'],
  ['\0vite/modulepreload-polyfill.js', 'vite'],
  ['\0vite/preload-helper.js', 'vite'],
]);

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
 */
export function collectNotices(root, bundle) {
  root = realpathSync(root);
  const manifest = JSON.parse(readFileSync(resolve(root, 'licenses/manifest.json'), 'utf8'));
  const lock = JSON.parse(readFileSync(resolve(root, 'package-lock.json'), 'utf8'));
  const packages = new Set();
  const fontPaths = new Set();
  let katexRuntime = false;
  let sveltePromise = false;

  /** @param {string} id */
  function origin(id) {
    if (virtualPackages.has(id)) {
      const name = virtualPackages.get(id);
      packages.add(name);
      return name;
    }
    if (id.includes('\0')) throw new Error(`Uncovered virtual license origin: ${id}`);
    const name = inside(root, realpathSync(resolve(root, id.split('?')[0])));
    if (name.startsWith('node_modules/')) {
      const match = /^node_modules\/((?:@[^/]+\/)?[^/]+)\//.exec(name);
      const packageName = match?.[1];
      if (!packageName || !manifest.packages[packageName] || name.includes('/node_modules/')) {
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
      if (!item.originalFileNames.length) throw new Error(`Asset has no license origins: ${item.fileName}`);
      for (const id of item.originalFileNames) owners.add(origin(id));
      // Vite combines the imported KaTeX stylesheet with project styles.
      if (item.fileName.endsWith('.css') && packages.has('katex')) owners.add('katex');
    } else {
      if (item.originalFileNames.length !== 1) throw new Error(`Uncovered copied asset: ${item.fileName}`);
      const path = inside(root, realpathSync(resolve(root, item.originalFileNames[0])));
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

  const sections = ['Third-party notices for the Sandbox Science workspace GUI.\n' +
    'Includes runtime helpers and complete upstream notice files; some upstream files also cover build-only code.\n'];
  const packageRecords = [];
  for (const name of [...packages].sort()) {
    const approved = manifest.packages[name];
    const installed = JSON.parse(readFileSync(resolve(root, `node_modules/${name}/package.json`), 'utf8'));
    const locked = lock.packages[`node_modules/${name}`];
    if (!approved || !locked || installed.name !== name || installed.version !== approved.version ||
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
  files.push({ file: 'THIRD-PARTY-NOTICES.txt', sha256: sha256(notices),
    notices: [...packages, ...(fontPaths.size ? ['KaTeX fonts (OFL-1.1)'] : [])].sort() });
  files.push({ file: 'LICENSE-Sandbox-Science.txt', sha256: sha256(project), notices: ['Sandbox Science'] });
  files.sort((a, b) => a.file < b.file ? -1 : a.file > b.file ? 1 : 0);
  return {
    'THIRD-PARTY-NOTICES.txt': notices,
    'LICENSE-Sandbox-Science.txt': project,
    'license-manifest.json': JSON.stringify({ protocol: 1, packages: packageRecords,
      fontAndSvgLicense: fontPaths.size || katexRuntime ? manifest.ofl : null,
      reactLicense: katexRuntime ? manifest.reactLicense : null,
      isPromiseLicense: sveltePromise ? manifest.svelteEmbedded.upstream : null, files }, null, 2) + '\n',
  };
}

/** Include checked runtime and asset notices in the production GUI distribution. @returns {import('vite').Plugin} */
export function licenseNotices() {
  let root;
  return {
    name: 'workspace-license-notices', apply: 'build', enforce: 'post',
    configResolved(config) {
      root = config.root;
      if (config.publicDir && existsSync(config.publicDir) && readdirSync(config.publicDir).length) {
        throw new Error('Public assets need explicit license coverage before copying');
      }
    },
    generateBundle: { order: 'post', handler(_, bundle) {
      for (const [fileName, source] of Object.entries(collectNotices(root, bundle))) {
        this.emitFile({ type: 'asset', fileName, source });
      }
    } },
  };
}
