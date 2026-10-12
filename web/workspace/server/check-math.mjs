// SPDX-License-Identifier: MIT
// Trusted one-formula launcher. Python supplies its hard deadline and bounded pipes.
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const MAX_INPUT = 262_144;
const [major, minor] = process.versions.node.split('.').map(Number);
if (major < 22 || (major === 22 && minor < 12)) throw new Error('Node 22.12 or newer required');
let size = 0;
const parts = [];
for await (const chunk of process.stdin) {
  size += chunk.length;
  if (size > MAX_INPUT) throw new Error('math request too large');
  parts.push(chunk);
}
const input = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(parts)));
if (input === null || typeof input !== 'object' || Array.isArray(input)
    || Object.keys(input).sort().join(',') !== 'display,id,macros,tex'
    || typeof input.id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(input.id)
    || typeof input.tex !== 'string' || input.tex.length > 20_000
    || typeof input.display !== 'boolean'
    || input.macros === null || typeof input.macros !== 'object' || Array.isArray(input.macros)
    || Object.entries(input.macros).length > 200
    || Object.entries(input.macros).some(([name, value]) => name.length > 100
      || typeof value !== 'string' || value.length > 1_000)) {
  throw new Error('invalid math request');
}
// The bundle is operator configuration, never a document or model-supplied path.
if (process.argv.length !== 3) throw new Error('math bundle path required');
const { checkFormula } = await import(pathToFileURL(resolve(process.argv[2])).href);
const result = JSON.stringify({ ...checkFormula(input), runtime: process.versions.node });
if (Buffer.byteLength(result) > 32_768) throw new Error('math result too large');
process.stdout.write(result + '\n');
