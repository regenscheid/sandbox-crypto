# KaTeX: tryCombineChars removes merged symbols one splice at a time

**Status.** Kept here, not filed upstream: filing is the user's call. Found on 2026-10-11 by the adversarial review of the workspace's math repairs (PR #61), with a CPU profile, and measured again for this note. The workspace no longer meets it: it shows a formula that defines a macro as written, and without definitions a formula's symbols are bounded by its length (docs/WORKSPACE.md, "Showing untrusted text").

**Affects.** KaTeX 0.18.9 (npm), `tryCombineChars` in its build of `buildCommon` (`dist/katex.mjs`, line 4901 in that release).

## Summary

`tryCombineChars` merges each run of adjacent symbol nodes that can share a span into the first of them. It removes each merged node with `chars.splice(i + 1, 1)` inside its loop, and every splice shifts the rest of the array, so a run of n mergeable symbols costs time proportional to n². A formula's length does not bound n: within KaTeX's `maxExpand` (1,000 here), a few nested `\def`s expand a short formula into hundreds of thousands of symbols.

## Steps to reproduce

```js
import katex from 'katex';

const body = 'x'.repeat(400);
const defs = `\\def\\a{${body}}\\def\\b{${'\\a'.repeat(10)}}\\def\\c{${'\\b'.repeat(10)}}`;
const tex = defs + '\\c'.repeat(4); // 472 characters, 160,000 symbols after expansion
console.time('render');
katex.renderToString(tex, { displayMode: true, maxExpand: 1000, throwOnError: true });
console.timeEnd('render');
```

**Expected.** Time in proportion to the 160,000 symbols: well under a second, as for 5,000 symbols written out (about 25 ms).

**Actual.** 17 s with Node 24 on babbage. With 20,000 symbols it takes 0.5 s, and with 320,000 (eight `\c`) 35 s. A browser tab rendering such a formula freezes for as long, since KaTeX renders synchronously.

## Suggested fix

Build the merged list in one pass: keep a write index, and append each symbol either to the last kept node (when `canCombine` allows) or as a new kept node, then truncate the array. That is linear in the number of symbols and keeps the current output.
