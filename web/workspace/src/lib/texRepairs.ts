// SPDX-License-Identifier: MIT
/**
 * The deterministic repairs of LaTeX that KaTeX rejects (`repairTex`), chosen by KaTeX's
 * error. Adapted from cscience-crypto's src/web/cscience-math.js, contributed under the
 * MIT License by its author. Pure: the server-side math checker uses it too.
 */

import { safeRepair } from './texDefinitions';

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
