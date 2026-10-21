// SPDX-License-Identifier: MIT
/**
 * Highlighting for the files the artifact viewer shows as text: a small lexer per language
 * splits a text into tagged parts, which the page draws as text with a class each, never as
 * HTML (the file's text is the agents'). Formatting JSON re-indents its tokens and keeps
 * each number and string exactly as written: parsing it would round integers past 2^53.
 */

export type TokenKind =
  | 'plain'
  | 'comment'
  | 'string'
  | 'number'
  | 'keyword'
  | 'literal'
  | 'builtin'
  | 'function'
  | 'key'
  | 'variable'
  | 'punctuation';

export interface Token {
  kind: TokenKind;
  text: string;
}

export type Language = 'python' | 'json' | 'shell' | 'c' | 'tex' | 'plain';

/** Past this many characters a text is shown plain: drawing every part would be slow. */
export const HIGHLIGHT_LIMIT = 200_000;

const BY_EXTENSION: Record<string, Language> = {
  py: 'python',
  sage: 'python',
  json: 'json',
  jsonl: 'json',
  sh: 'shell',
  bash: 'shell',
  c: 'c',
  h: 'c',
  rs: 'c',
  tex: 'tex',
};

/** The language a file's name says it is in; `plain` when it says none we know. */
export function languageOf(path: string): Language {
  const extension = /\.([A-Za-z0-9]+)$/.exec(path)?.[1]?.toLowerCase();
  return (extension && BY_EXTENSION[extension]) || 'plain';
}

const words = (list: string) => new Set(list.split(' '));

const PYTHON_KEYWORDS = words(
  'and as assert async await break class continue def del elif else except finally for ' +
    'from global if import in is lambda nonlocal not or pass raise return try while with yield',
);
const PYTHON_LITERALS = words('True False None');
const PYTHON_BUILTINS = words(
  'abs all any bin bool bytes chr dict divmod enumerate filter float format frozenset ' +
    'getattr hasattr hex int isinstance iter len list map max min next object oct open ord ' +
    'pow print range repr reversed round set setattr sorted str sum super tuple type zip self ' +
    // Sage, as the agents' scripts use it.
    'matrix vector GF ZZ QQ RR PolynomialRing load identity_matrix zero_matrix block_matrix',
);
const SHELL_KEYWORDS = words(
  'if then else elif fi for while until do done case esac in function select return export ' +
    'local readonly set unset',
);
const SHELL_BUILTINS = words('echo printf cd exit source test read eval exec trap shift wait');
const C_KEYWORDS = words(
  'auto break case const continue default do else enum extern for goto if inline register ' +
    'restrict return sizeof static struct switch typedef union volatile while as async await ' +
    'crate dyn fn impl let loop match mod move mut pub ref Self self trait type unsafe use where',
);
const C_LITERALS = words('true false NULL None Some Ok Err');
const C_BUILTINS = words(
  'void char short int long float double signed unsigned bool size_t uint8_t uint16_t ' +
    'uint32_t uint64_t int8_t int16_t int32_t int64_t u8 u16 u32 u64 u128 usize i8 i16 i32 ' +
    'i64 i128 isize f32 f64 str String Vec Option Result Box',
);

type Rule = [TokenKind, RegExp];

const NUMBER =
  /(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?[jJ]?)/y;
const IDENTIFIER = /[A-Za-z_][A-Za-z0-9_]*/y;

const RULES: Record<Exclude<Language, 'plain'>, Rule[]> = {
  python: [
    ['comment', /#[^\n]*/y],
    ['string', /[rRbBuUfF]{0,2}(?:"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$))/y],
    ['string', /[rRbBuUfF]{0,2}(?:"(?:[^"\\\n]|\\.)*"?|'(?:[^'\\\n]|\\.)*'?)/y],
    ['function', /@[A-Za-z_][\w.]*/y],
    ['number', NUMBER],
  ],
  json: [
    ['string', /"(?:[^"\\\n]|\\.)*"?/y],
    ['number', /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y],
    ['punctuation', /[{}[\],:]/y],
  ],
  shell: [
    ['comment', /(?<![^\s;])#[^\n]*/y],
    ['string', /'[^']*'?|"(?:[^"\\]|\\[\s\S])*"?/y],
    ['variable', /\$(?:\{[^}\n]*\}?|[A-Za-z_]\w*|[0-9@#?*$!-])/y],
    ['number', /\d+(?![\w.])/y],
  ],
  c: [
    ['comment', /\/\/[^\n]*|\/\*[\s\S]*?(?:\*\/|$)/y],
    ['string', /"(?:[^"\\\n]|\\.)*"?|'(?:[^'\\\n]|\\.){1,2}'/y],
    ['function', /#\s*[A-Za-z]+/y],
    ['number', NUMBER],
  ],
  tex: [
    ['comment', /(?<!\\)%[^\n]*/y],
    ['keyword', /\\(?:[A-Za-z@]+|.)/y],
    ['punctuation', /[{}[\]$&^_]/y],
  ],
};

function word(language: Language, text: string, next: string, previous: string): TokenKind {
  const called = /^\s*\(/.test(next);
  switch (language) {
    case 'python':
      if (PYTHON_KEYWORDS.has(text)) return 'keyword';
      if (PYTHON_LITERALS.has(text)) return 'literal';
      if (previous === 'def' || previous === 'class') return 'function';
      if (PYTHON_BUILTINS.has(text)) return 'builtin';
      return called ? 'function' : 'plain';
    case 'json':
      return ['true', 'false', 'null'].includes(text) ? 'literal' : 'plain';
    case 'shell':
      if (SHELL_KEYWORDS.has(text)) return 'keyword';
      return SHELL_BUILTINS.has(text) ? 'builtin' : 'plain';
    case 'c':
      if (C_KEYWORDS.has(text)) return 'keyword';
      if (C_LITERALS.has(text)) return 'literal';
      if (C_BUILTINS.has(text)) return 'builtin';
      if (previous === 'fn') return 'function';
      return called ? 'function' : 'plain';
    default:
      return 'plain';
  }
}

/**
 * A text split into tagged parts, which joined give the text back exactly. A JSON string
 * followed by a colon is a key. Plain parts next to each other are joined.
 */
export function highlight(text: string, language: Language): Token[] {
  if (language === 'plain' || text.length > HIGHLIGHT_LIMIT) return [{ kind: 'plain', text }];
  const rules = RULES[language];
  const tokens: Token[] = [];
  let previousWord = '';
  const push = (kind: TokenKind, part: string) => {
    const last = tokens.at(-1);
    if (kind === 'plain' && last?.kind === 'plain') last.text += part;
    else tokens.push({ kind, text: part });
  };
  let position = 0;
  outer: while (position < text.length) {
    for (const [kind, pattern] of rules) {
      pattern.lastIndex = position;
      const match = pattern.exec(text);
      if (match && match[0].length > 0) {
        let tagged = kind;
        if (
          language === 'json' &&
          kind === 'string' &&
          /^\s*:/.test(text.slice(position + match[0].length, position + match[0].length + 64))
        )
          tagged = 'key';
        push(tagged, match[0]);
        position += match[0].length;
        continue outer;
      }
    }
    IDENTIFIER.lastIndex = position;
    const name = IDENTIFIER.exec(text);
    if (name) {
      const after = text.slice(position + name[0].length, position + name[0].length + 32);
      push(word(language, name[0], after, previousWord), name[0]);
      previousWord = name[0];
      position += name[0].length;
      continue;
    }
    const character = text[position]!;
    if (!/\s/.test(character)) previousWord = '';
    push('plain', character);
    position += 1;
  }
  return tokens;
}

/**
 * JSON laid out two spaces an indent, each number and string as written; null when the
 * text is not one JSON value, so the page shows it as saved.
 */
export function formatJson(text: string): string | null {
  const tokens = highlight(text, 'json');
  const parts: Token[] = [];
  for (const token of tokens) {
    if (token.kind === 'plain') {
      if (token.text.trim() !== '') return null;
      continue;
    }
    parts.push(token);
  }
  if (parts.length === 0) return null;
  let out = '';
  let depth = 0;
  let done = false;
  const line = () => '\n' + '  '.repeat(depth);
  for (let index = 0; index < parts.length; index++) {
    const { kind, text: part } = parts[index]!;
    // One value: anything after it, as in JSON lines, is shown as saved.
    if (done) return null;
    if (kind === 'string' && !part.endsWith('"')) return null;
    if (part === '{' || part === '[') {
      const close = part === '{' ? '}' : ']';
      if (parts[index + 1]?.text === close) {
        out += part + close;
        index += 1;
        done = depth === 0;
        continue;
      }
      depth += 1;
      out += part + line();
    } else if (part === '}' || part === ']') {
      depth -= 1;
      if (depth < 0) return null;
      out += line() + part;
      done = depth === 0;
    } else if (part === ',') out += ',' + line();
    else if (part === ':') out += ': ';
    else {
      out += part;
      done = depth === 0;
    }
  }
  return depth === 0 ? out + '\n' : null;
}
