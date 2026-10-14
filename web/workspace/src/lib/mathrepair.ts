// SPDX-License-Identifier: MIT
/**
 * Repairs for LaTeX that KaTeX cannot render, and a plain-text stand-in when nothing
 * repairs it (D65; docs/WORKSPACE.md, "Math repairs").
 *
 * Adapted from cscience-crypto's src/web/cscience-math.js, contributed under the MIT
 * License by its author.
 *
 * When KaTeX rejects an expression, the renderer (markdown.ts) tries, in order:
 *
 * 1. the deterministic repairs (`repairTex`), chosen by KaTeX's error: commands KaTeX
 *    lacks, labels and references, unbalanced braces and `\left`/`\right`, bare `%`, `#`
 *    and `&`, double and dangling scripts, alignment outside an environment, display-only
 *    environments in inline math, and unknown commands as operator names;
 * 2. the reviewer model's repair, when the server offers one. It is asked for once per
 *    expression, in the background, and the page remembers the answer (`ModelRepairs`);
 *    the text is rendered again when it arrives;
 * 3. a plain-text approximation in Unicode (`texToUnicode`).
 *
 * Whatever is shown is marked with how it was repaired, and the expression as written is
 * kept beside it, to read and copy. The text itself never changes.
 */
import { api, type MathRepairAnswer, type MathRepairRequest } from './api';
import { visible } from './text';

/** The longest error message kept, from KaTeX or the server. */
const MAX_ERROR_LENGTH = 300;
/** The longest expression the server repairs (mathrepair.py, `MAX_TEX`). */
export const MAX_MODEL_TEX = 4_000;

// ---------------------------------------------------------------------------------------
// 1. Deterministic repairs
// ---------------------------------------------------------------------------------------

/** A replacement: a template with `$1`, or a function of the match and its groups. */
type Replacement = string | ((match: string, groups: (string | undefined)[]) => string);

const MACRO_REPLACEMENTS: [RegExp, Replacement][] = [
  [/\\bm\{/g, '\\boldsymbol{'],
  [/\\(?:mbox|hbox|textnormal|textsc|textup|textrm)\{/g, '\\text{'],
  [/\\emph\{/g, '\\textit{'],
  [/\\ensuremath\{/g, '{'],
  [/\\(?:sfrac|nicefrac)\{/g, '\\frac{'],
  [/\\abs\{([^{}]*)\}/g, '\\lvert $1\\rvert'],
  [/\\norm\{([^{}]*)\}/g, '\\lVert $1\\rVert'],
  [/\\(?:label|footnote|vspace|hspace\*)\{[^{}]*\}/g, ''],
  [
    /\\(?:nonumber|notag|noindent|centering|allowbreak|smallskip|medskip|bigskip|hline)(?![A-Za-z])/g,
    '',
  ],
  [/\\hfill(?![A-Za-z])/g, '\\quad'],
  [/\\newline(?![A-Za-z])/g, '\\\\'],
  [/\\intertext\{([^{}]*)\}/g, '\\\\ \\text{$1} \\\\'],
  [/\\eqref\{([^{}]*)\}/g, '(\\text{$1})'],
  [/\\ref\{([^{}]*)\}/g, '\\text{$1}'],
  [/\\cite\{([^{}]*)\}/g, '[\\text{$1}]'],
  [/\\(?:R|N|Z|Q|C|F)(?![A-Za-z])/g, (match) => `\\mathbb{${match.slice(1)}}`],
  [/\\eps(?![A-Za-z])/g, '\\varepsilon'],
  [
    /\\begin\{(subequations|small|footnotesize|center)\}|\\end\{(subequations|small|footnotesize|center)\}/g,
    '',
  ],
  [/\\begin\{tabular\}/g, '\\begin{array}'],
  [/\\end\{tabular\}/g, '\\end{array}'],
  [/\\begin\{([pbvBV]?matrix)\*\}\{[^{}]*\}/g, '\\begin{$1}'],
  [/\\end\{([pbvBV]?matrix)\*\}/g, '\\end{$1}'],
  [/\\begin\{eqnarray\*?\}/g, '\\begin{aligned}'],
  [/\\end\{eqnarray\*?\}/g, '\\end{aligned}'],
  [/\\begin\{multline\*?\}/g, '\\begin{gathered}'],
  [/\\end\{multline\*?\}/g, '\\end{gathered}'],
];

const INLINE_ONLY_ENVIRONMENTS: [RegExp, string][] = [
  [/\\begin\{(?:align|alignat|flalign)\*?\}(\{[^{}]*\})?/g, '\\begin{aligned}'],
  [/\\end\{(?:align|alignat|flalign)\*?\}/g, '\\end{aligned}'],
  [/\\begin\{equation\*?\}/g, '{'],
  [/\\end\{equation\*?\}/g, '}'],
  [/\\begin\{gather\*?\}/g, '\\begin{gathered}'],
  [/\\end\{gather\*?\}/g, '\\end{gathered}'],
];

const SCRIPT_TOKEN = String.raw`(\{[^{}]*\}|\\[A-Za-z]+|[^\s{}_^\\])`;
const DOUBLE_SUBSCRIPT = new RegExp(`_${SCRIPT_TOKEN}_${SCRIPT_TOKEN}`, 'g');
const DOUBLE_SUPERSCRIPT = new RegExp(`\\^${SCRIPT_TOKEN}\\^${SCRIPT_TOKEN}`, 'g');

/** A letter of a command's name, as KaTeX's tokenizer reads them. */
const LETTER = /[A-Za-z@]/;

/** Whether the backslash at `offset` is escaped: an odd number of backslashes precede it. */
function escapedAt(source: string, offset: number): boolean {
  let count = 0;
  for (let at = offset - 1; at >= 0 && source.charCodeAt(at) === 0x5c; at -= 1) count += 1;
  return count % 2 === 1;
}

/**
 * `pattern`, which matches a command from its backslash, replaced only where that
 * backslash starts a command: `\\R` is a line break and an R, and stays. A removal never
 * joins a command to the letters after it: `\d\label{x}ef` becomes `\d ef`, not `\def`.
 */
function replaceCommands(tex: string, pattern: RegExp, replacement: Replacement): string {
  return tex.replace(pattern, (...args: unknown[]) => {
    const match = args[0] as string;
    const offset = args[args.length - 2] as number;
    const source = args[args.length - 1] as string;
    if (escapedAt(source, offset)) return match;
    const groups = args.slice(1, -2) as (string | undefined)[];
    const out =
      typeof replacement === 'string'
        ? replacement.replace(/\$(\d)/g, (_all, digit: string) => groups[Number(digit) - 1] ?? '')
        : replacement(match, groups);
    const before = source[offset - 1] ?? '';
    const after = source[offset + match.length] ?? '';
    return out === '' && LETTER.test(before) && LETTER.test(after) ? ' ' : out;
  });
}

/** How many times `pattern` matches a command from its backslash, escaped ones left out. */
function countCommands(tex: string, pattern: RegExp): number {
  let count = 0;
  for (const match of tex.matchAll(pattern)) if (!escapedAt(tex, match.index)) count += 1;
  return count;
}

function applyMacroReplacements(tex: string): string {
  let out = tex;
  for (const [pattern, replacement] of MACRO_REPLACEMENTS) {
    out = replaceCommands(out, pattern, replacement);
  }
  return out;
}

/** `_ ^ % # $ &` inside `\text{…}` are characters there, so they are escaped. */
function escapeTextArguments(tex: string): string {
  return replaceCommands(
    tex,
    /(\\text(?:it|bf|sf|tt)?)\{([^{}]*)\}/g,
    (_match, [command, body]) =>
      `${command ?? ''}{${(body ?? '').replace(/(^|[^\\])([_^%#$&])/g, '$1\\$2')}}`,
  );
}

function balanceBraces(tex: string): string {
  let depth = 0;
  let out = '';
  for (let index = 0; index < tex.length; index += 1) {
    const char = tex[index]!;
    if (char === '\\') {
      out += char + (tex[index + 1] ?? '');
      index += 1;
      continue;
    }
    if (char === '{') depth += 1;
    else if (char === '}') {
      if (depth === 0) continue;
      depth -= 1;
    }
    out += char;
  }
  return out + '}'.repeat(depth);
}

function balanceDelimiters(tex: string): string {
  const lefts = countCommands(tex, /\\left(?![A-Za-z])/g);
  const rights = countCommands(tex, /\\right(?![A-Za-z])/g);
  if (lefts > rights) return tex + ' \\right.'.repeat(lefts - rights);
  if (rights > lefts) return '\\left. '.repeat(rights - lefts) + tex;
  return tex;
}

function escapeBareCharacters(tex: string): string {
  const defines = /\\(?:newcommand|renewcommand|providecommand|def)(?![A-Za-z])/.test(tex);
  return tex
    .replace(/(^|[^\\])%/g, '$1\\%')
    .replace(/(^|[^\\])#/g, defines ? '$1#' : '$1\\#')
    .replace(/(^|[^\\])\$/g, '$1');
}

function fixDoubleScripts(tex: string): string {
  return tex.replace(DOUBLE_SUBSCRIPT, '_{$1_$2}').replace(DOUBLE_SUPERSCRIPT, '^{$1^$2}');
}

function stripDanglingScripts(tex: string): string {
  return tex.replace(/[_^]\s*$/, '').replace(/[_^](?=\s*[)\]])/g, '');
}

function wrapAlignment(tex: string): string {
  const hasAmpersand = /(^|[^\\])&/.test(tex);
  const hasBreak = /\\\\/.test(tex);
  if (/\\begin\{/.test(tex) || (!hasAmpersand && !hasBreak)) return tex;
  const environment = hasAmpersand ? 'aligned' : 'gathered';
  const body = tex.trim().replace(/\\\\\s*$/, '');
  return `\\begin{${environment}}${body}\\end{${environment}}`;
}

function inlineEnvironments(tex: string, displayMode: boolean): string {
  if (displayMode) return tex;
  let out = tex;
  for (const [pattern, replacement] of INLINE_ONLY_ENVIRONMENTS) {
    out = replaceCommands(out, pattern, replacement);
  }
  return out;
}

function operatorForUnknownMacro(tex: string, errorText: string): string {
  const match = /Undefined control sequence: \\([A-Za-z]+)/.exec(errorText);
  if (!match) return tex;
  const name = match[1]!;
  const pattern = new RegExp(`\\\\${name}(?![A-Za-z])`, 'g');
  return replaceCommands(tex, pattern, `\\operatorname{${name}}`);
}

function basicRepairs(tex: string, displayMode: boolean): string {
  return escapeTextArguments(inlineEnvironments(applyMacroReplacements(tex), displayMode));
}

function structuralRepairs(tex: string): string {
  let out = balanceBraces(tex);
  out = balanceDelimiters(out);
  out = escapeBareCharacters(out);
  out = fixDoubleScripts(out);
  out = stripDanglingScripts(out);
  return wrapAlignment(out);
}

function targetedRepairs(tex: string, errorText: string, displayMode: boolean): string {
  let out = tex;
  if (/Undefined control sequence/.test(errorText)) out = operatorForUnknownMacro(out, errorText);
  if (/Double (?:sub|super)script/.test(errorText)) out = fixDoubleScripts(out);
  if (/Expected '\}'|Unexpected end of input|got 'EOF'|Expected group/.test(errorText)) {
    out = stripDanglingScripts(balanceBraces(out));
  }
  if (/\\left|\\right|Missing \\right|Missing \\left/.test(errorText)) out = balanceDelimiters(out);
  const alignment =
    /got '&'|Can't use function '\\\\'|only in display mode|misplaced &|Misplaced &/;
  if (alignment.test(errorText)) {
    out = wrapAlignment(inlineEnvironments(out, displayMode));
  }
  if (/Unexpected character|Expected 'EOF'/.test(errorText)) out = escapeBareCharacters(out);
  return out;
}

/**
 * Repaired versions of `tex` to try, best first: the repairs KaTeX's error points to, the
 * substitutions alone, then every structural repair. Each differs from `tex`, and none
 * brings in a command the renderer refuses or that KaTeX's trust setting gates
 * (`safeRepair`).
 */
export function repairTex(tex: string, errorText: string, displayMode = false): string[] {
  if (typeof tex !== 'string') return [];
  const trimmed = tex.trim();
  const basic = basicRepairs(trimmed, displayMode);
  const candidates = [
    targetedRepairs(basic, errorText || '', displayMode),
    basic,
    structuralRepairs(basic),
  ];
  const unique: string[] = [];
  for (const candidate of candidates) {
    const value = candidate.trim();
    if (!value || value === trimmed || unique.includes(value)) continue;
    if (!safeRepair(trimmed, value)) continue;
    unique.push(value);
  }
  return unique;
}

/**
 * Commands that define macros. A formula with one is not rendered, nor is a document's
 * macro that has one (macros.ts): macros come from a document's front matter and the
 * project (living documents, LD-28), and a definition can make KaTeX's work grow far
 * beyond the formula's length within its limit on expansions.
 */
const DEFINITIONS: ReadonlySet<string> = new Set([
  'def',
  'gdef',
  'edef',
  'xdef',
  'let',
  'futurelet',
  'global',
  'long',
  'outer',
  'newcommand',
  'renewcommand',
  'providecommand',
  'DeclareRobustCommand',
  'DeclareMathOperator',
  'newenvironment',
  'renewenvironment',
]);

/** Commands that KaTeX runs only when trusted; the renderer never trusts. */
const TRUSTED_ONLY: ReadonlySet<string> = new Set([
  'href',
  'url',
  'includegraphics',
  'htmlClass',
  'htmlId',
  'htmlStyle',
  'htmlData',
]);

/** Commands whose link KaTeX reads with `%` as a character rather than a comment. */
const LINK_ARGUMENTS: readonly string[] = ['href', 'url', 'includegraphics'];

// KaTeX's token pattern, from KaTeX 0.18.9's src/Lexer.ts (MIT License, Copyright (c)
// 2013-2020 Khan Academy and other contributors), with its two `\verb` alternatives left
// out: `verbEnd` finds the same `\verb` tokens through an index, in linear time, where the
// pattern's lazy search takes time quadratic in the number of openers that never close.
// Its groups: 1 whitespace, 2 a backslash before whitespace, 3 any other token, 4 the name
// of a control word. Sticky, as KaTeX's lexer only takes a match where it stands.
const SPACE = '[ \r\n\t]';
const CONTROL_WORD = '\\\\[a-zA-Z@]+';
const CONTROL_SYMBOL = '\\\\[^\uD800-\uDFFF]';
const CONTROL_WORD_WHITESPACE = `(${CONTROL_WORD})${SPACE}*`;
const CONTROL_SPACE = '\\\\(\n|[ \r\t]+\n?)[ \r\t]*';
const COMBINING_MARKS = '[\u0300-\u036F]';
const TOKEN = new RegExp(
  `(${SPACE}+)|` +
    `${CONTROL_SPACE}|` +
    `([!-\\[\\]-\u2027\u202A-\uD7FF\uF900-\uFFFF]${COMBINING_MARKS}*` +
    `|[\uD800-\uDBFF][\uDC00-\uDFFF]${COMBINING_MARKS}*` +
    `|${CONTROL_WORD_WHITESPACE}` +
    `|${CONTROL_SYMBOL})`,
  'y',
);

/** Whether `code` ends a line as JavaScript's `.` reads lines, where a `\verb` body ends. */
function endsLine(code: number): boolean {
  return code === 0x0a || code === 0x0d || code === 0x2028 || code === 0x2029;
}

/**
 * For each position of `tex`, where its character next occurs (`next`), and where the
 * next line ends (`lineEnd`, one longer), or -1: so that each `\verb` finds its closing
 * delimiter at once, and openers that never close take linear time, not quadratic.
 */
interface VerbIndex {
  next: Int32Array;
  lineEnd: Int32Array;
}

function verbIndex(tex: string): VerbIndex {
  const next = new Int32Array(tex.length);
  const lineEnd = new Int32Array(tex.length + 1);
  const seen = new Map<number, number>();
  lineEnd[tex.length] = -1;
  for (let index = tex.length - 1; index >= 0; index -= 1) {
    const code = tex.charCodeAt(index);
    next[index] = seen.get(code) ?? -1;
    seen.set(code, index);
    lineEnd[index] = endsLine(code) ? index : lineEnd[index + 1]!;
  }
  return { next, lineEnd };
}

/**
 * Where the `\verb|…|` or `\verb*|…|` that starts at `index` ends, or -1 when none starts
 * there: KaTeX's two `\verb` alternatives. After `\verb`, any character but `*` and a
 * letter is the delimiter (after `\verb*`, any at all), and the body runs to the same
 * character on the same line. Without that, `\verb` is a command, which KaTeX rejects.
 */
function verbEnd(tex: string, index: number, verbs: VerbIndex): number {
  if (!tex.startsWith('\\verb', index)) return -1;
  let at = index + 5;
  const starred = tex.charCodeAt(at) === 0x2a;
  if (starred) at += 1;
  if (at >= tex.length) return -1;
  if (!starred && (tex.charCodeAt(at) === 0x2a || /[A-Za-z]/.test(tex[at]!))) return -1;
  // The delimiter's next occurrence, if no line ends before it (it may end the line itself).
  const close = verbs.next[at]!;
  const lineEnd = verbs.lineEnd[at + 1]!;
  return close !== -1 && (lineEnd === -1 || close <= lineEnd) ? close + 1 : -1;
}

/**
 * The names of the control words in `tex`, token by token as KaTeX's lexer reads it: a
 * comment starts only at a token that is `%` alone (`%` with a combining mark after it is
 * another token, which KaTeX may skip unparsed, as `\@secondoftwo` does its argument),
 * and runs to the next line feed; a `\verb` is one token. Where KaTeX's lexer would stop
 * with an error, this reads on, a character later: more names, never fewer.
 */
function lexedNames(tex: string): Set<string> {
  const names = new Set<string>();
  const verbs = tex.includes('\\verb') ? verbIndex(tex) : null;
  let position = 0;
  while (position < tex.length) {
    if (verbs !== null && tex.charCodeAt(position) === 0x5c) {
      const end = verbEnd(tex, position, verbs);
      if (end !== -1) {
        position = end;
        continue;
      }
    }
    TOKEN.lastIndex = position;
    const match = TOKEN.exec(tex);
    if (match === null) {
      position += 1;
      continue;
    }
    position = TOKEN.lastIndex;
    const name = match[4];
    if (name !== undefined) {
      names.add(name.slice(1));
    } else if (match[3] === '%') {
      const newline = tex.indexOf('\n', position);
      if (newline === -1) break;
      position = newline + 1;
    }
  }
  return names;
}

/** Every name after a backslash in `tex`, wherever the backslash stands. */
function everyName(tex: string): Set<string> {
  const names = new Set<string>();
  for (const match of tex.matchAll(/\\([A-Za-z@]+)/g)) names.add(match[1]!);
  return names;
}

/**
 * The names of the commands in `tex`, as KaTeX reads them (`lexedNames`). Only in the link
 * of `\href`, `\url` or `\includegraphics` does KaTeX read a `%` as a character rather
 * than a comment, and which `%` that is depends on how it parses the formula. So in a
 * formula with one of them and a `%`, every name after a backslash counts, however the
 * formula is read: more names, never fewer.
 */
function commandNames(tex: string): Set<string> {
  const names = lexedNames(tex);
  if (tex.includes('%') && LINK_ARGUMENTS.some((name) => names.has(name))) {
    for (const name of everyName(tex)) names.add(name);
  }
  return names;
}

/** The first command in `tex` that defines a macro, or null. */
export function definesMacros(tex: string): string | null {
  for (const name of commandNames(tex)) if (DEFINITIONS.has(name)) return name;
  return null;
}

/**
 * Whether a repair of `original` is one to show: it defines no macro, and it brings in no
 * command that KaTeX gates by trust that the original did not have. Removals can join the
 * pieces of one (`\hr\label{a}ef` would become `\href`), and a model can be steered.
 */
export function safeRepair(original: string, repair: string): boolean {
  const before = commandNames(original);
  for (const name of commandNames(repair)) {
    if (DEFINITIONS.has(name)) return false;
    if (TRUSTED_ONLY.has(name) && !before.has(name)) return false;
  }
  return true;
}

/** A map that keeps its most recently used entries, within a count and a total weight. */
export class Kept<V> {
  readonly #map = new Map<string, V>();
  #weight = 0;

  constructor(
    private readonly maxEntries: number,
    private readonly maxWeight = Number.POSITIVE_INFINITY,
    private readonly weigh: (key: string, value: V) => number = () => 0,
  ) {}

  get size(): number {
    return this.#map.size;
  }

  /** The sum of the entries' weights. */
  get weight(): number {
    return this.#weight;
  }

  has(key: string): boolean {
    return this.#map.has(key);
  }

  get(key: string): V | undefined {
    const value = this.#map.get(key);
    if (value !== undefined) {
      this.#map.delete(key);
      this.#map.set(key, value);
    }
    return value;
  }

  set(key: string, value: V): void {
    const old = this.#map.get(key);
    if (old !== undefined) {
      this.#map.delete(key);
      this.#weight -= this.weigh(key, old);
    }
    const weight = this.weigh(key, value);
    // An entry heavier than the whole limit is not kept, and leaves the others be.
    if (weight > this.maxWeight) return;
    this.#map.set(key, value);
    this.#weight += weight;
    while (this.#map.size > this.maxEntries || this.#weight > this.maxWeight) {
      const [oldestKey, oldestValue] = this.#map.entries().next().value!;
      this.#map.delete(oldestKey);
      this.#weight -= this.weigh(oldestKey, oldestValue);
    }
  }

  clear(): void {
    this.#map.clear();
    this.#weight = 0;
  }
}

/**
 * The candidates and approximations already worked out, for the page's re-renders, within
 * a count and a size in characters: a candidate can be five times its formula's length.
 */
const keptCandidates = new Kept<string[]>(2_000, 2_000_000, (key, candidates) =>
  candidates.reduce((sum, candidate) => sum + candidate.length, key.length),
);
const keptApproximations = new Kept<string>(
  2_000,
  1_000_000,
  (key, unicode) => key.length + unicode.length,
);

function candidatesFor(tex: string, error: string, displayMode: boolean): string[] {
  const key = `${displayMode ? 'display' : 'inline'}\u0000${error}\u0000${tex}`;
  let candidates = keptCandidates.get(key);
  if (candidates === undefined) {
    candidates = repairTex(tex, error, displayMode);
    keptCandidates.set(key, candidates);
  }
  return candidates;
}

function approximationOf(tex: string): string {
  let unicode = keptApproximations.get(tex);
  if (unicode === undefined) {
    unicode = texToUnicode(tex) || tex;
    keptApproximations.set(tex, unicode);
  }
  return unicode;
}

// ---------------------------------------------------------------------------------------
// 2. A plain-text approximation, for when nothing renders
// ---------------------------------------------------------------------------------------

/** Lookups that ignore what objects inherit, so that `\constructor` is only a name. */
function own<T>(table: Record<string, T>, key: string): T | undefined {
  return Object.hasOwn(table, key) ? table[key] : undefined;
}

const SYMBOLS: Record<string, string> = {
  alpha: 'α',
  beta: 'β',
  gamma: 'γ',
  delta: 'δ',
  epsilon: 'ϵ',
  varepsilon: 'ε',
  zeta: 'ζ',
  eta: 'η',
  theta: 'θ',
  vartheta: 'ϑ',
  iota: 'ι',
  kappa: 'κ',
  lambda: 'λ',
  mu: 'μ',
  nu: 'ν',
  xi: 'ξ',
  pi: 'π',
  varpi: 'ϖ',
  rho: 'ρ',
  varrho: 'ϱ',
  sigma: 'σ',
  varsigma: 'ς',
  tau: 'τ',
  upsilon: 'υ',
  phi: 'ϕ',
  varphi: 'φ',
  chi: 'χ',
  psi: 'ψ',
  omega: 'ω',
  Gamma: 'Γ',
  Delta: 'Δ',
  Theta: 'Θ',
  Lambda: 'Λ',
  Xi: 'Ξ',
  Pi: 'Π',
  Sigma: 'Σ',
  Upsilon: 'Υ',
  Phi: 'Φ',
  Psi: 'Ψ',
  Omega: 'Ω',
  le: '≤',
  leq: '≤',
  leqslant: '⩽',
  ge: '≥',
  geq: '≥',
  geqslant: '⩾',
  ne: '≠',
  neq: '≠',
  ll: '≪',
  gg: '≫',
  approx: '≈',
  equiv: '≡',
  sim: '∼',
  simeq: '≃',
  cong: '≅',
  propto: '∝',
  prec: '≺',
  succ: '≻',
  infty: '∞',
  pm: '±',
  mp: '∓',
  times: '×',
  cdot: '·',
  div: '÷',
  ast: '∗',
  star: '⋆',
  circ: '∘',
  bullet: '•',
  oplus: '⊕',
  otimes: '⊗',
  ominus: '⊖',
  odot: '⊙',
  oslash: '⊘',
  wedge: '∧',
  land: '∧',
  vee: '∨',
  lor: '∨',
  neg: '¬',
  lnot: '¬',
  cap: '∩',
  cup: '∪',
  sqcup: '⊔',
  sqcap: '⊓',
  in: '∈',
  notin: '∉',
  ni: '∋',
  subset: '⊂',
  subseteq: '⊆',
  subsetneq: '⊊',
  supset: '⊃',
  supseteq: '⊇',
  setminus: '∖',
  emptyset: '∅',
  varnothing: '∅',
  forall: '∀',
  exists: '∃',
  nexists: '∄',
  to: '→',
  rightarrow: '→',
  longrightarrow: '⟶',
  leftarrow: '←',
  gets: '←',
  leftrightarrow: '↔',
  Rightarrow: '⇒',
  implies: '⇒',
  Leftarrow: '⇐',
  Leftrightarrow: '⇔',
  iff: '⇔',
  mapsto: '↦',
  uparrow: '↑',
  downarrow: '↓',
  hookrightarrow: '↪',
  sum: '∑',
  prod: '∏',
  coprod: '∐',
  int: '∫',
  iint: '∬',
  iiint: '∭',
  oint: '∮',
  partial: '∂',
  nabla: '∇',
  ldots: '…',
  cdots: '⋯',
  dots: '…',
  dotsc: '…',
  dotsb: '⋯',
  vdots: '⋮',
  ddots: '⋱',
  langle: '⟨',
  rangle: '⟩',
  lfloor: '⌊',
  rfloor: '⌋',
  lceil: '⌈',
  rceil: '⌉',
  lvert: '|',
  rvert: '|',
  vert: '|',
  lVert: '‖',
  rVert: '‖',
  Vert: '‖',
  mid: '|',
  parallel: '∥',
  perp: '⊥',
  angle: '∠',
  top: '⊤',
  bot: '⊥',
  models: '⊨',
  vdash: '⊢',
  dashv: '⊣',
  hbar: 'ℏ',
  ell: 'ℓ',
  Re: 'ℜ',
  Im: 'ℑ',
  aleph: 'ℵ',
  prime: '′',
  degree: '°',
  therefore: '∴',
  because: '∵',
  triangle: '△',
  square: '□',
  Box: '□',
  diamond: '⋄',
  bigcup: '⋃',
  bigcap: '⋂',
  bigoplus: '⨁',
  bigotimes: '⨂',
  bigvee: '⋁',
  bigwedge: '⋀',
  qed: '∎',
  blacksquare: '■',
  checkmark: '✓',
  mathbb: '',
  colon: ':',
  backslash: '\\',
  lbrace: '{',
  rbrace: '}',
  lbrack: '[',
  rbrack: ']',
  dagger: '†',
  ddagger: '‡',
  S: '§',
  P: '¶',
  copyright: '©',
  pounds: '£',
  euro: '€',
  yen: '¥',
  mathsterling: '£',
};

const FUNCTION_NAMES = new Set([
  'sin',
  'cos',
  'tan',
  'cot',
  'sec',
  'csc',
  'arcsin',
  'arccos',
  'arctan',
  'sinh',
  'cosh',
  'tanh',
  'coth',
  'log',
  'ln',
  'lg',
  'exp',
  'min',
  'max',
  'lim',
  'liminf',
  'limsup',
  'sup',
  'inf',
  'det',
  'dim',
  'ker',
  'deg',
  'gcd',
  'lcm',
  'Pr',
  'arg',
  'hom',
  'mod',
  'bmod',
  'sgn',
  'tr',
  'rank',
]);

const SPACING: Record<string, string> = {
  ',': ' ',
  ';': ' ',
  ':': ' ',
  '!': '',
  ' ': ' ',
  quad: '  ',
  qquad: '    ',
  enspace: ' ',
  thinspace: ' ',
  medspace: ' ',
  thickspace: ' ',
  negthinspace: '',
};

const SUBSCRIPTS: Record<string, string> = {
  0: '₀',
  1: '₁',
  2: '₂',
  3: '₃',
  4: '₄',
  5: '₅',
  6: '₆',
  7: '₇',
  8: '₈',
  9: '₉',
  '+': '₊',
  '-': '₋',
  '=': '₌',
  '(': '₍',
  ')': '₎',
  a: 'ₐ',
  e: 'ₑ',
  h: 'ₕ',
  i: 'ᵢ',
  j: 'ⱼ',
  k: 'ₖ',
  l: 'ₗ',
  m: 'ₘ',
  n: 'ₙ',
  o: 'ₒ',
  p: 'ₚ',
  r: 'ᵣ',
  s: 'ₛ',
  t: 'ₜ',
  u: 'ᵤ',
  v: 'ᵥ',
  x: 'ₓ',
};

const SUPERSCRIPTS: Record<string, string> = {
  0: '⁰',
  1: '¹',
  2: '²',
  3: '³',
  4: '⁴',
  5: '⁵',
  6: '⁶',
  7: '⁷',
  8: '⁸',
  9: '⁹',
  '+': '⁺',
  '-': '⁻',
  '=': '⁼',
  '(': '⁽',
  ')': '⁾',
  a: 'ᵃ',
  b: 'ᵇ',
  c: 'ᶜ',
  d: 'ᵈ',
  e: 'ᵉ',
  f: 'ᶠ',
  g: 'ᵍ',
  h: 'ʰ',
  i: 'ⁱ',
  j: 'ʲ',
  k: 'ᵏ',
  l: 'ˡ',
  m: 'ᵐ',
  n: 'ⁿ',
  o: 'ᵒ',
  p: 'ᵖ',
  r: 'ʳ',
  s: 'ˢ',
  t: 'ᵗ',
  u: 'ᵘ',
  v: 'ᵛ',
  w: 'ʷ',
  x: 'ˣ',
  y: 'ʸ',
  z: 'ᶻ',
  T: 'ᵀ',
};

const ACCENTS: Record<string, string> = {
  hat: '\u0302',
  widehat: '\u0302',
  bar: '\u0304',
  overline: '\u0305',
  tilde: '\u0303',
  widetilde: '\u0303',
  vec: '\u20D7',
  dot: '\u0307',
  ddot: '\u0308',
  check: '\u030C',
  breve: '\u0306',
  acute: '\u0301',
  grave: '\u0300',
  underline: '\u0332',
  mathring: '\u030A',
};

const DOUBLE_STRUCK = 'A𝔸B𝔹CℂD𝔻E𝔼F𝔽G𝔾HℍI𝕀J𝕁K𝕂L𝕃M𝕄NℕO𝕆PℙQℚRℝS𝕊T𝕋U𝕌V𝕍W𝕎X𝕏Y𝕐Zℤ';
const SCRIPT = 'A𝒜BℬC𝒞D𝒟EℰFℱG𝒢HℋIℐJ𝒥K𝒦LℒMℳN𝒩O𝒪P𝒫Q𝒬RℛS𝒮T𝒯U𝒰V𝒱W𝒲X𝒳Y𝒴Z𝒵';
const FRAKTUR = 'A𝔄B𝔅CℭD𝔇E𝔈F𝔉G𝔊HℌIℑJ𝔍K𝔎L𝔏M𝔐N𝔑O𝔒P𝔓Q𝔔RℜS𝔖T𝔗U𝔘V𝔙W𝔚X𝔛Y𝔜Zℨ';

function alphabetMap(table: string): Record<string, string> {
  const map: Record<string, string> = {};
  const chars = Array.from(table);
  for (let index = 0; index < chars.length; index += 2) map[chars[index]!] = chars[index + 1]!;
  return map;
}

const ALPHABETS: Record<string, Record<string, string>> = {
  mathbb: alphabetMap(DOUBLE_STRUCK),
  mathcal: alphabetMap(SCRIPT),
  mathscr: alphabetMap(SCRIPT),
  mathfrak: alphabetMap(FRAKTUR),
};

function boldChar(char: string): string {
  const code = char.codePointAt(0)!;
  if (code >= 65 && code <= 90) return String.fromCodePoint(0x1d400 + code - 65);
  if (code >= 97 && code <= 122) return String.fromCodePoint(0x1d41a + code - 97);
  if (code >= 48 && code <= 57) return String.fromCodePoint(0x1d7ce + code - 48);
  return char;
}

function styleText(text: string, style: string): string {
  if (style === 'mathbf' || style === 'boldsymbol' || style === 'textbf') {
    return Array.from(text, boldChar).join('');
  }
  const map = own(ALPHABETS, style);
  if (!map) return text;
  return Array.from(text, (char) => own(map, char) ?? char).join('');
}

function scriptText(text: string, table: Record<string, string>, marker: string): string {
  const chars = Array.from(text);
  if (chars.length > 0 && chars.every((char) => own(table, char) !== undefined)) {
    return chars.map((char) => table[char]!).join('');
  }
  if (chars.length === 1) return `${marker}${text}`;
  return `${marker}(${text})`;
}

function accentText(text: string, combining: string): string {
  return Array.from(text, (char) => (/\s/.test(char) ? char : char + combining)).join('');
}

function isSimpleOperand(text: string): boolean {
  return /^[\p{L}\p{N}⁺⁻⁼₊₋₌′]+$/u.test(text);
}

const BEGIN = /\\begin\{([A-Za-z*]+)\}/g;
const ARGUMENT = /\{[^{}]*\}/y;

/**
 * The first environment, as the original's pattern
 * `\\begin\{([A-Za-z*]+)\}(\{[^{}]*\})?([\s\S]*?)\\end\{\1\}` finds it: the leftmost
 * `\begin{name}` followed by an `\end{name}`, with the nearest such end. Found with
 * searches rather than that pattern, whose backtracking is quadratic on long texts.
 */
function firstEnvironment(
  tex: string,
): { start: number; end: number; name: string; body: string } | null {
  const ends = new Map<string, number>();
  BEGIN.lastIndex = 0;
  for (let match = BEGIN.exec(tex); match !== null; match = BEGIN.exec(tex)) {
    const name = match[1]!;
    const after = match.index + match[0].length;
    const endTag = `\\end{${name}}`;
    let endAt = ends.get(name);
    if (endAt === undefined || (endAt !== -1 && endAt < after)) {
      endAt = tex.indexOf(endTag, after);
      ends.set(name, endAt);
    }
    if (endAt === -1) continue;
    ARGUMENT.lastIndex = after;
    const argument = ARGUMENT.exec(tex);
    const bodyStart = argument && after + argument[0].length <= endAt ? ARGUMENT.lastIndex : after;
    const body = tex.slice(bodyStart, endAt);
    return { start: match.index, end: endAt + endTag.length, name, body };
  }
  return null;
}

function transliterateEnvironments(tex: string): string {
  let out = tex;
  for (let guard = 0; guard < 50; guard += 1) {
    const found = firstEnvironment(out);
    if (!found) break;
    const name = found.name.replace(/\*$/, '');
    const rows = found.body
      .split(/\\\\|\\cr(?![A-Za-z])/)
      .map((row) => row.replace(/\\hline/g, '').trim())
      .filter(Boolean);
    let replacement: string;
    if (/matrix$|^array$|^smallmatrix$/.test(name)) {
      const cells = rows.map((row) =>
        row
          .split(/(?<!\\)&/)
          .map((cell) => cell.trim())
          .join(' '),
      );
      const brackets: Record<string, [string, string]> = {
        pmatrix: ['(', ')'],
        bmatrix: ['[', ']'],
        Bmatrix: ['\\lbrace ', ' \\rbrace'],
        vmatrix: ['|', '|'],
        Vmatrix: ['‖', '‖'],
      };
      const [open, close] = own(brackets, name) ?? ['', ''];
      replacement = `${open}${cells.join('; ')}${close}`;
    } else if (name === 'cases' || name === 'dcases' || name === 'rcases') {
      const lines = rows.map((row) => {
        const [value, condition] = row.split(/(?<!\\)&/).map((part) => part.trim());
        return condition ? `${value} if ${condition}` : (value ?? '');
      });
      replacement = `\\lbrace ${lines.join('; ')} \\rbrace`;
    } else {
      replacement = rows
        .map((row) =>
          row
            .replace(/(?<!\\)&/g, ' ')
            .replace(/\s+/g, ' ')
            .trim(),
        )
        .join('; ');
    }
    out = `${out.slice(0, found.start)}{${replacement}}${out.slice(found.end)}`;
  }
  return out;
}

type TexToken =
  | { type: 'command'; name: string }
  | { type: '{' }
  | { type: '}' }
  | { type: '^' }
  | { type: '_' }
  | { type: 'char'; value: string };

type TexNode =
  | { type: 'text'; value: string }
  | { type: 'group'; children: TexNode[] }
  | { type: 'sup' | 'sub'; arg: TexNode[] }
  | { type: 'command'; name: string; args: TexNode[][]; index?: TexNode[] };

function tokenize(tex: string): TexToken[] {
  const tokens: TexToken[] = [];
  let index = 0;
  while (index < tex.length) {
    const char = tex[index]!;
    if (char === '\\') {
      const next = tex[index + 1] ?? '';
      if (/[A-Za-z]/.test(next)) {
        let end = index + 1;
        while (end < tex.length && /[A-Za-z]/.test(tex[end]!)) end += 1;
        if (tex[end] === '*') end += 1;
        tokens.push({ type: 'command', name: tex.slice(index + 1, end) });
        index = end;
      } else {
        tokens.push({ type: 'command', name: next });
        index += 2;
      }
      continue;
    }
    if (char === '{' || char === '}' || char === '^' || char === '_') {
      tokens.push({ type: char });
      index += 1;
      continue;
    }
    tokens.push({ type: 'char', value: char });
    index += 1;
  }
  return tokens;
}

interface ParseState {
  index: number;
}

function parseGroup(tokens: TexToken[], state: ParseState): TexNode[] {
  const nodes: TexNode[] = [];
  while (state.index < tokens.length) {
    const token = tokens[state.index]!;
    if (token.type === '}') {
      state.index += 1;
      return nodes;
    }
    state.index += 1;
    if (token.type === '{') {
      nodes.push({ type: 'group', children: parseGroup(tokens, state) });
    } else if (token.type === '^' || token.type === '_') {
      nodes.push({ type: token.type === '^' ? 'sup' : 'sub', arg: parseArgument(tokens, state) });
    } else if (token.type === 'command') {
      nodes.push(parseCommand(token.name, tokens, state));
    } else {
      nodes.push({ type: 'text', value: token.value });
    }
  }
  return nodes;
}

function parseArgument(tokens: TexToken[], state: ParseState): TexNode[] {
  for (;;) {
    const token = tokens[state.index];
    if (token?.type !== 'char' || !/\s/.test(token.value)) break;
    state.index += 1;
  }
  const token = tokens[state.index];
  if (!token) return [];
  state.index += 1;
  if (token.type === '{') return parseGroup(tokens, state);
  if (token.type === 'command') return [parseCommand(token.name, tokens, state)];
  if (token.type === 'char') return [{ type: 'text', value: token.value }];
  return [];
}

const ARGUMENT_COUNTS: Record<string, number> = {
  frac: 2,
  dfrac: 2,
  tfrac: 2,
  cfrac: 2,
  binom: 2,
  dbinom: 2,
  tbinom: 2,
  overset: 2,
  underset: 2,
  stackrel: 2,
  sqrt: 1,
  text: 1,
  textit: 1,
  textbf: 1,
  textsf: 1,
  texttt: 1,
  mathrm: 1,
  mathit: 1,
  mathbf: 1,
  mathsf: 1,
  mathtt: 1,
  boldsymbol: 1,
  mathbb: 1,
  mathcal: 1,
  mathscr: 1,
  mathfrak: 1,
  operatorname: 1,
  'operatorname*': 1,
  pmod: 1,
  boxed: 1,
  hat: 1,
  widehat: 1,
  bar: 1,
  overline: 1,
  tilde: 1,
  widetilde: 1,
  vec: 1,
  dot: 1,
  ddot: 1,
  check: 1,
  breve: 1,
  acute: 1,
  grave: 1,
  underline: 1,
  mathring: 1,
  textcolor: 2,
  color: 1,
  phantom: 1,
  hphantom: 1,
  vphantom: 1,
  mathop: 1,
  mathbin: 1,
  mathrel: 1,
  mathopen: 1,
  mathclose: 1,
  mathord: 1,
  mathpunct: 1,
  substack: 1,
  xrightarrow: 1,
  xleftarrow: 1,
  overbrace: 1,
  underbrace: 1,
  cancel: 1,
  tag: 1,
  'tag*': 1,
  textstyle: 0,
  displaystyle: 0,
  scriptstyle: 0,
  scriptscriptstyle: 0,
};

function parseCommand(name: string, tokens: TexToken[], state: ParseState): TexNode {
  const next = tokens[state.index];
  if (name === 'sqrt' && next?.type === 'char' && next.value === '[') {
    let end = state.index + 1;
    const index: TexNode[] = [];
    for (; end < tokens.length; end += 1) {
      const token = tokens[end]!;
      if (token.type === 'char' && token.value === ']') break;
      // The original read braces here as commands without names, and showed "undefined".
      if (token.type === 'char') index.push({ type: 'text', value: token.value });
      else if (token.type === 'command') {
        index.push({ type: 'command', name: token.name, args: [] });
      } else if (token.type === '^' || token.type === '_') {
        index.push({ type: 'text', value: token.type });
      }
    }
    state.index = end + 1;
    return { type: 'command', name, args: [parseArgument(tokens, state)], index };
  }
  const count = own(ARGUMENT_COUNTS, name) ?? 0;
  const args: TexNode[][] = [];
  for (let position = 0; position < count; position += 1) args.push(parseArgument(tokens, state));
  return { type: 'command', name, args };
}

function renderNodes(nodes: TexNode[]): string {
  let out = '';
  for (const node of nodes) out += renderNode(node);
  return out;
}

function renderNode(node: TexNode): string {
  switch (node.type) {
    case 'text':
      return node.value;
    case 'group':
      return renderNodes(node.children);
    case 'sub':
      return scriptText(renderNodes(node.arg).trim(), SUBSCRIPTS, '_');
    case 'sup':
      return scriptText(renderNodes(node.arg).trim(), SUPERSCRIPTS, '^');
    case 'command':
      return renderCommand(node);
  }
}

const DROPPED = new Set([
  'left',
  'right',
  'displaystyle',
  'textstyle',
  'scriptstyle',
  'limits',
  'nolimits',
  'phantom',
  'hphantom',
  'vphantom',
  'tag',
  'tag*',
  'nonumber',
  'notag',
]);

const AS_TEXT = new Set([
  'operatorname',
  'operatorname*',
  'text',
  'textit',
  'textsf',
  'texttt',
  'mathrm',
  'mathit',
  'mathsf',
  'mathtt',
  'mathop',
  'mathbin',
  'mathrel',
  'mathopen',
  'mathclose',
  'mathord',
  'mathpunct',
]);

function renderCommand(node: Extract<TexNode, { type: 'command' }>): string {
  const { name, args } = node;
  const arg = (position: number): string => renderNodes(args[position] ?? []).trim();
  const spacing = own(SPACING, name);
  if (spacing !== undefined) return spacing;
  if (DROPPED.has(name) || /^(?:big|Big|bigg|Bigg)[lr]?$/.test(name)) return '';
  if (name === 'frac' || name === 'dfrac' || name === 'tfrac' || name === 'cfrac') {
    const wrap = (text: string): string => (isSimpleOperand(text) ? text : `(${text})`);
    return `${wrap(arg(0))}/${wrap(arg(1))}`;
  }
  if (name === 'binom' || name === 'dbinom' || name === 'tbinom') return `C(${arg(0)}, ${arg(1)})`;
  if (name === 'sqrt') {
    const radicand = arg(0);
    const index = node.index ? renderNodes(node.index).trim() : '';
    const prefix = index ? scriptText(index, SUPERSCRIPTS, '^') : '';
    return `${prefix}√${isSimpleOperand(radicand) ? radicand : `(${radicand})`}`;
  }
  if (name === 'overset' || name === 'stackrel') {
    return `${arg(1)}${scriptText(arg(0), SUPERSCRIPTS, '^')}`;
  }
  if (name === 'underset') return `${arg(1)}${scriptText(arg(0), SUBSCRIPTS, '_')}`;
  if (name === 'pmod') return `(mod ${arg(0)})`;
  if (name === 'bmod') return 'mod';
  if (name === 'boxed' || name === 'cancel') return `[${arg(0)}]`;
  if (name === 'textcolor') return arg(1);
  if (name === 'color') return '';
  if (name === 'xrightarrow') return `→(${arg(0)})`;
  if (name === 'xleftarrow') return `←(${arg(0)})`;
  if (name === 'overbrace' || name === 'underbrace' || name === 'substack') return arg(0);
  if (AS_TEXT.has(name)) return arg(0);
  if (name === 'mathbf' || name === 'boldsymbol' || name === 'textbf' || own(ALPHABETS, name)) {
    return styleText(arg(0), name);
  }
  const accent = own(ACCENTS, name);
  if (accent !== undefined) return accentText(arg(0), accent);
  const symbol = own(SYMBOLS, name);
  if (symbol) return symbol;
  if (FUNCTION_NAMES.has(name)) return name;
  if (name === '\\') return '; ';
  return name;
}

/** Whitespace as the original's last steps leave it, in one pass rather than quadratic ones. */
function tidy(text: string): string {
  const parts = text.split(';');
  const joined = parts
    .map((part, index) => {
      let out = part;
      if (index > 0) out = out.trimStart();
      if (index < parts.length - 1) out = out.trimEnd();
      return out;
    })
    .join('; ');
  return joined
    .replace(/\s+/g, (run) => (run.includes('\n') ? ' ' : run.replace(/[ \t]+/g, ' ')))
    .trim();
}

/** A readable approximation of `tex` in Unicode, for when KaTeX renders nothing. */
export function texToUnicode(tex: string): string {
  if (typeof tex !== 'string') return '';
  try {
    const prepared = transliterateEnvironments(tex.replace(/\r\n?/g, '\n'));
    return tidy(renderNodes(parseGroup(tokenize(prepared), { index: 0 })));
  } catch {
    // Nesting deep enough to exhaust the stack: the source is the best approximation.
    return tex;
  }
}

// ---------------------------------------------------------------------------------------
// 3. The ladder: deterministic repairs, the model's repair, the approximation
// ---------------------------------------------------------------------------------------

/** What renders an expression and throws when it cannot: KaTeX, or a stand-in in tests. */
export interface MathRenderer {
  renderToString(tex: string, options: Record<string, unknown>): string;
}

/**
 * What a renderer throws when KaTeX has no time left in this go (markdown.ts): the
 * expression is not one that nothing repairs, only one to try again in a later go.
 */
export class OutOfTime extends Error {
  constructor() {
    super('KaTeX has no time left in this go');
    this.name = 'OutOfTime';
  }
}

/** What the page knows of one expression's model repair. */
export type ModelRepair = { tex: string; model: string | null } | { tex: null; reason: string };

export type Transport = (request: MathRepairRequest) => Promise<MathRepairAnswer>;

/** An expression recovered, as it is to be shown. */
export type ShownRecovery =
  | {
      status: 'repaired';
      /** `heuristic` for the deterministic repairs, `model` for the reviewer model's. */
      how: 'heuristic' | 'model';
      /** KaTeX's rendering of the repaired expression. */
      html: string;
      tex: string;
      model: string | null;
      error: string;
    }
  | {
      status: 'fallback';
      unicode: string;
      error: string;
      /** `pending` while the model's repair is awaited. */
      state: 'pending' | 'unrepairable';
      /** Why the model's repair is missing, when it is known. */
      reason: string | null;
    };

/**
 * An expression recovered, or `unfinished`: KaTeX had no time in this go for the repairs
 * that might render it, so it is to be recovered in a later go, and no model's repair is
 * asked for until then.
 */
export type Recovery = ShownRecovery | { status: 'unfinished' };

export interface RecoverOptions {
  displayMode: boolean;
  /** KaTeX's error for the expression as written. */
  error: unknown;
  /**
   * KaTeX's settings, with the renderer's limits on sizes and expansions; the ladder adds
   * `displayMode` and `throwOnError: true`, and always sets `trust: false`.
   */
  settings: Record<string, unknown>;
  /** The page's model repairs; without them, none are asked for. */
  repairs?: ModelRepairs | null;
  /** Renders a model's repair to check it; by default the renderer the ladder uses. */
  check?: MathRenderer;
  /** The key of the macros the expression renders with (macros.ts); none by default. */
  macros?: string;
  /** Exact project/effective context for server-side cache isolation. */
  scope?: Pick<MathRepairRequest, 'project' | 'macro_context'>;
}

function cut(text: string, length: number): string {
  let out = text.slice(0, length);
  const last = out.charCodeAt(out.length - 1);
  if (last >= 0xd800 && last <= 0xdbff) out = out.slice(0, -1); // half a surrogate pair
  return out;
}

/** KaTeX's error, or any other, in a few words. */
export function describeError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error ?? 'unknown error');
  const brief = message
    .replace(/^KaTeX parse error:\s*/, '')
    .replace(/\s+/g, ' ')
    .trim();
  return cut(brief, MAX_ERROR_LENGTH);
}

/**
 * The page's key for an expression's model repair, and for KaTeX's results: under
 * `macros`, the key of the macros in force (macros.ts), a formula renders otherwise.
 */
export function repairKey(tex: string, displayMode: boolean, macros = ''): string {
  const key = `${displayMode ? 'display' : 'inline'}\u0000${tex}`;
  return macros ? `${macros}\u0000${key}` : key;
}

/**
 * An expression KaTeX rejected, recovered: the first deterministic repair KaTeX renders,
 * else the model's repair if the page has one, else the Unicode approximation, with the
 * model's repair asked for when it can be. When the renderer runs out of time before
 * trying them all (`OutOfTime`), the expression is `unfinished`: a repair it did not try
 * may render it in a later go, and the model is not asked for it meanwhile.
 */
export function recoverMath(katex: MathRenderer, tex: string, options: RecoverOptions): Recovery {
  const displayMode = options.displayMode;
  const error = describeError(options.error);
  const settings = { ...options.settings, displayMode, throwOnError: true, trust: false };
  const render = (renderer: MathRenderer, candidate: string): string =>
    renderer.renderToString(candidate, settings);
  let cutShort = false;
  /** KaTeX's rendering of `candidate`, or null when it fails or there is no time for it. */
  const attempt = (candidate: string): string | null => {
    try {
      return render(katex, candidate);
    } catch (failure) {
      if (failure instanceof OutOfTime) cutShort = true;
      return null;
    }
  };

  for (const candidate of candidatesFor(tex, error, displayMode)) {
    const html = attempt(candidate);
    if (html !== null) {
      return { status: 'repaired', how: 'heuristic', html, tex: candidate, model: null, error };
    }
  }

  const repairs = options.repairs ?? null;
  const key = repairKey(tex, displayMode, options.macros);
  const known = repairs?.get(key);
  if (known?.tex && safeRepair(tex, known.tex)) {
    // Not renderable here, or beyond this text's budget, it falls through.
    const html = attempt(known.tex);
    if (html !== null) {
      return { status: 'repaired', how: 'model', html, tex: known.tex, model: known.model, error };
    }
  }
  if (cutShort) return { status: 'unfinished' };
  let state: 'pending' | 'unrepairable' = 'unrepairable';
  if (repairs !== null && known === undefined) {
    const check = options.check ?? katex;
    repairs.request(key, { tex, display: displayMode, error, ...options.scope }, (candidate) => {
      if (!safeRepair(tex, candidate)) throw new Error('it brings in a command that is off here');
      render(check, candidate);
    });
    if (repairs.isPending(key)) state = 'pending';
  }
  const settled = repairs?.get(key);
  const reason = settled && settled.tex === null ? settled.reason : (repairs?.unavailable ?? null);
  return { status: 'fallback', unicode: approximationOf(tex), error, state, reason };
}

/** How a page asks for model repairs; the defaults suit a browser. */
export interface ModelRepairsOptions {
  /** Requests at once, once the server has said it has a model; one until then. */
  maxActive?: number;
  /** Runs `callback` before the next frame: listeners hear of answers once a frame. */
  nextFrame?: (callback: () => void) => void;
  /** Runs `callback` after `ms` milliseconds; returns a function that cancels it. */
  later?: (callback: () => void, ms: number) => () => void;
  /** The waits before each new try of a request the server was too busy for. */
  busyRetries?: readonly number[];
}

/**
 * Before the next frame; within a second where frames do not come, as in a hidden tab,
 * which runs none (the text is then rendered again at most once a second).
 */
function nextFrame(callback: () => void): void {
  if (typeof requestAnimationFrame !== 'function') {
    setTimeout(callback, 0);
    return;
  }
  let done = false;
  const run = (): void => {
    if (done) return;
    done = true;
    callback();
  };
  const timer = setTimeout(run, 1_000);
  requestAnimationFrame(() => {
    clearTimeout(timer);
    run();
  });
}

function later(callback: () => void, ms: number): () => void {
  const timer = setTimeout(callback, ms);
  return () => clearTimeout(timer);
}

interface Job {
  key: string;
  request: MathRepairRequest;
  check: (tex: string) => void;
  /** Tries the server was too busy for. */
  busy: number;
}

/** A job's next try, while it waits for it. */
interface Retry {
  cancel: () => void;
}

/** The repairs one page keeps: within a count, and a size in characters. */
const MAX_KEPT_REPAIRS = 2_000;
const MAX_KEPT_REPAIR_WEIGHT = 2_000_000;

function repairWeight(key: string, repair: ModelRepair): number {
  return key.length + (repair.tex === null ? repair.reason.length : repair.tex.length);
}

/**
 * The reviewer model's repairs for one page: asked for in the background, once per
 * expression, and remembered until the page reloads, the least recently used dropped
 * first beyond `MAX_KEPT_REPAIRS` (the server keeps its answers too, so asking again for
 * one dropped costs no model call).
 *
 * Until the server first says it has a model, one request is in flight at a time; then
 * two. When it says it has none, or the session ends or reaches its limit, the requests
 * still waiting are dropped at once, and so is any answered later as `busy`. A request
 * the server was too busy for is tried again later, a few times. Listeners hear of the
 * expressions whose answers arrived once a frame, so that the texts showing them render
 * again at most once a frame.
 */
export class ModelRepairs {
  readonly #cache = new Kept<ModelRepair>(MAX_KEPT_REPAIRS, MAX_KEPT_REPAIR_WEIGHT, repairWeight);
  /** Expressions asked for and not answered: waiting, in flight, or waiting to try again. */
  readonly #pending = new Set<string>();
  readonly #queue: Job[] = [];
  readonly #retries = new Map<string, Retry>();
  readonly #listeners = new Set<(keys: ReadonlySet<string>) => void>();
  readonly #arrived = new Set<string>();
  readonly #idle: (() => void)[] = [];
  readonly #maxActive: number;
  readonly #nextFrame: (callback: () => void) => void;
  readonly #later: (callback: () => void, ms: number) => () => void;
  readonly #busyRetries: readonly number[];
  #active = 0;
  #pumping = false;
  #telling = false;
  /** Whether the server has said it has a model. */
  #enabled = false;
  /** Why the page asks no more: the server has no model, the session ended, or its limit. */
  #off: string | null = null;

  constructor(
    private readonly transport: Transport | null,
    options: ModelRepairsOptions = {},
  ) {
    this.#maxActive = options.maxActive ?? 2;
    this.#nextFrame = options.nextFrame ?? nextFrame;
    this.#later = options.later ?? later;
    this.#busyRetries = options.busyRetries ?? [2_000, 4_000, 8_000];
  }

  /** Why no repairs are asked for, or null while they are. */
  get unavailable(): string | null {
    if (this.transport === null) return 'model repairs are not available here';
    return this.#off;
  }

  get(key: string): ModelRepair | undefined {
    return this.#cache.get(key);
  }

  /** Remember a repair, as if the model had answered it. */
  set(key: string, repair: ModelRepair): void {
    this.#cache.set(key, repair);
  }

  isPending(key: string): boolean {
    return this.#pending.has(key);
  }

  /** Ask for a repair of the expression under `key`, unless it is known or asked for. */
  request(key: string, request: MathRepairRequest, check: (tex: string) => void): void {
    if (this.transport === null || this.#off !== null) return;
    if (this.#cache.has(key) || this.#pending.has(key)) return;
    if (request.tex.length > MAX_MODEL_TEX) {
      this.#cache.set(key, { tex: null, reason: 'too long to ask the model for a repair' });
      return;
    }
    this.#pending.add(key);
    this.#queue.push({ key, request, check, busy: 0 });
    // Requests start after the rendering that asks for them.
    if (!this.#pumping) {
      this.#pumping = true;
      queueMicrotask(() => {
        this.#pumping = false;
        this.#pump();
      });
    }
  }

  #pump(): void {
    const transport = this.transport;
    if (transport === null) return;
    // One at a time until the server says it has a model: with none, its first answer
    // turns repairs off, and the rest are never sent.
    const limit = this.#enabled ? this.#maxActive : 1;
    while (this.#off === null && this.#active < limit) {
      const job = this.#queue.shift();
      if (job === undefined) break;
      this.#active += 1;
      void this.#ask(transport, job);
    }
  }

  async #ask(transport: Transport, job: Job): Promise<void> {
    let outcome: ModelRepair | 'busy' | 'off';
    try {
      outcome = this.#accept(await transport(job.request), job.check);
    } catch (error) {
      outcome = this.#failed(error);
    }
    this.#active -= 1;
    // A `busy` that arrives after another answer turned repairs off is not tried again.
    if (outcome === 'busy' && this.#off === null) this.#tryLater(job);
    else this.#settle(job.key, outcome === 'off' || outcome === 'busy' ? null : outcome);
    this.#pump();
  }

  #accept(answer: MathRepairAnswer, check: (tex: string) => void): ModelRepair | 'busy' | 'off' {
    if (!answer.enabled) {
      this.#turnOff(answer.reason || 'model repairs are off');
      return 'off';
    }
    this.#enabled = true;
    const candidate = typeof answer.tex === 'string' ? answer.tex.trim() : '';
    if (!candidate && answer.reason === 'busy') return 'busy';
    if (!candidate) return { tex: null, reason: answer.reason || 'no repair' };
    try {
      check(candidate);
    } catch (error) {
      return { tex: null, reason: `the repair does not render either: ${describeError(error)}` };
    }
    return { tex: candidate, model: answer.model ?? null };
  }

  #failed(error: unknown): ModelRepair | 'off' {
    const status = (error as { status?: unknown } | null)?.status;
    // Logged out, or this session's limit reached: asking again would fail the same way.
    if (status === 401 || status === 429) {
      this.#turnOff(describeError(error));
      return 'off';
    }
    return { tex: null, reason: describeError(error) };
  }

  /** Ask again after a wait, while the server is busy; after the last, give up. */
  #tryLater(job: Job): void {
    const wait = this.#busyRetries[job.busy];
    if (wait === undefined) {
      this.#settle(job.key, { tex: null, reason: 'the server stayed busy' });
      return;
    }
    job.busy += 1;
    const retry: Retry = { cancel: () => undefined };
    this.#retries.set(job.key, retry);
    retry.cancel = this.#later(() => {
      // Cancelled, or settled since: as when repairs went off.
      if (this.#retries.get(job.key) !== retry) return;
      this.#retries.delete(job.key);
      if (this.#off !== null) {
        this.#settle(job.key, null);
        return;
      }
      this.#queue.push(job);
      this.#pump();
    }, wait);
  }

  /** Repairs are off for this page: drop what waits, at once, and tell the listeners once. */
  #turnOff(reason: string): void {
    if (this.#off !== null) return;
    this.#off = reason;
    for (const job of this.#queue.splice(0)) this.#settle(job.key, null);
    const retries = [...this.#retries];
    this.#retries.clear();
    for (const [key, retry] of retries) {
      retry.cancel();
      this.#settle(key, null);
    }
  }

  /** `key` answered (`repair`, or nothing to keep); the listeners hear of it next frame. */
  #settle(key: string, repair: ModelRepair | null): void {
    if (repair !== null) this.#cache.set(key, repair);
    this.#pending.delete(key);
    this.#arrived.add(key);
    if (!this.#telling) {
      this.#telling = true;
      this.#nextFrame(() => this.#tell());
    }
  }

  #tell(): void {
    this.#telling = false;
    const keys: ReadonlySet<string> = new Set(this.#arrived);
    this.#arrived.clear();
    for (const listener of [...this.#listeners]) listener(keys);
    if (this.#pending.size === 0 && !this.#telling) {
      for (const resolve of this.#idle.splice(0)) resolve();
    }
  }

  /** Call `listener`, at most once a frame, with the expressions whose answers arrived. */
  subscribe(listener: (keys: ReadonlySet<string>) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  /** Every request asked for so far answered, and the listeners told. */
  settled(): Promise<void> {
    if (this.#pending.size === 0 && !this.#telling) return Promise.resolve();
    return new Promise((resolve) => this.#idle.push(resolve));
  }
}

/** The page's model repairs, through the workspace API; none outside a browser. */
export const pageRepairs = new ModelRepairs(
  typeof window === 'undefined' ? null : (request) => api.mathRepair(request),
);

// ---------------------------------------------------------------------------------------
// 4. How a recovered expression is shown
// ---------------------------------------------------------------------------------------

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** `text` ending as a sentence ends. */
function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

/**
 * A recovered expression as HTML: what renders, a button that says how it was repaired,
 * and, behind it, why and the expression as written (`written`, with its delimiters).
 * `tag` is `div` for a block; displayed math within a paragraph is a `span`. `label` is
 * an equation's label, as HTML. Everything from the text is escaped, and bidirectional
 * controls in it show as their code points: KaTeX's error quotes the expression.
 */
export function recoveredHtml(
  recovery: ShownRecovery,
  tag: 'span' | 'div',
  display: boolean,
  written: string,
  label = '',
): string {
  const layout = display ? 'math-display' : 'math-inline';
  let classes: string;
  let data: string;
  let shown: string;
  let mark: string;
  let message: string;
  if (recovery.status === 'repaired') {
    classes = `math ${layout} math-repaired`;
    data = `data-repair="${recovery.how}"`;
    shown = recovery.html;
    if (recovery.how === 'model') {
      const model = recovery.model ? ` (${recovery.model})` : '';
      mark = 'model-repaired';
      message = `Repaired by the reviewer model${model}.`;
    } else {
      mark = 'auto-repaired';
      message = 'Repaired automatically.';
    }
    message += ` KaTeX could not render it as written: ${sentence(recovery.error)}`;
  } else {
    classes = `math ${layout} math-approximated`;
    data = `data-repair="approximation" data-state="${recovery.state}"`;
    shown = `<span class="math-unicode">${escapeHtml(visible(recovery.unicode))}</span>`;
    mark = 'approximated';
    message =
      `KaTeX could not render this: ${sentence(recovery.error)} ` +
      'Shown as plain text, approximately.';
    if (recovery.state === 'pending') message += ' The reviewer model has been asked for a repair.';
    else if (recovery.reason) message += ` No repair: ${sentence(recovery.reason)}`;
  }
  message = visible(message);
  const title = `${message} Press to see the LaTeX as written.`;
  return (
    `<${tag} class="${classes}" ${data}>${shown}${label}` +
    `<button type="button" class="math-mark" aria-expanded="false" title="${escapeHtml(title)}">` +
    `${mark}</button>` +
    `<span class="math-details" hidden><span class="math-message">${escapeHtml(message)}</span> ` +
    `<code class="math-source">${escapeHtml(visible(written))}</code></span></${tag}>`
  );
}
