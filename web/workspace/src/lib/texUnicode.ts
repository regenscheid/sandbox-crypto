// SPDX-License-Identifier: MIT
/**
 * A readable approximation of a formula in Unicode (`texToUnicode`), for when KaTeX
 * renders nothing and no repair helps. Adapted from cscience-crypto's
 * src/web/cscience-math.js, contributed under the MIT License by its author. Pure.
 */

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
