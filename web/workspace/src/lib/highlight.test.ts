// SPDX-License-Identifier: MIT
import { describe, expect, it } from 'vitest';
import { formatJson, highlight, HIGHLIGHT_LIMIT, languageOf, type Token } from './highlight';

const kinds = (tokens: Token[]) =>
  tokens.filter((token) => token.kind !== 'plain').map((token) => [token.kind, token.text]);

describe('highlighting a file shown as text', () => {
  it('knows the language from the file name', () => {
    expect(languageOf('a/b/run.sage')).toBe('python');
    expect(languageOf('out.JSON')).toBe('json');
    expect(languageOf('go.sh')).toBe('shell');
    expect(languageOf('notes.txt')).toBe('plain');
  });

  it('gives the text back exactly, whatever the language', () => {
    const text = 'def f(x):\n  return "a\\"b" # c\n​$x {1: [true]}';
    for (const language of ['python', 'json', 'shell', 'c', 'tex', 'plain'] as const)
      expect(
        highlight(text, language)
          .map((token) => token.text)
          .join(''),
      ).toBe(text);
  });

  it('tags Python and Sage words, strings, numbers and comments', () => {
    const tokens = highlight(
      "# key1\ndef zmat(x):\n z=matrix(F,128,4)\n assert hashlib.sha256(b'x').hexdigest()==None",
      'python',
    );
    expect(kinds(tokens)).toEqual([
      ['comment', '# key1'],
      ['keyword', 'def'],
      ['function', 'zmat'],
      ['builtin', 'matrix'],
      ['number', '128'],
      ['number', '4'],
      ['keyword', 'assert'],
      ['function', 'sha256'],
      ['string', "b'x'"],
      ['function', 'hexdigest'],
      ['literal', 'None'],
    ]);
  });

  it('tells a JSON key from a string value, and leaves long texts plain', () => {
    expect(kinds(highlight('{"rank": 80, "ok": true, "name": "h2"}', 'json'))).toEqual([
      ['punctuation', '{'],
      ['key', '"rank"'],
      ['punctuation', ':'],
      ['number', '80'],
      ['punctuation', ','],
      ['key', '"ok"'],
      ['punctuation', ':'],
      ['literal', 'true'],
      ['punctuation', ','],
      ['key', '"name"'],
      ['punctuation', ':'],
      ['string', '"h2"'],
      ['punctuation', '}'],
    ]);
    expect(highlight('x'.repeat(HIGHLIGHT_LIMIT + 1), 'python')).toHaveLength(1);
  });
});

describe('formatting JSON', () => {
  it('lays it out two spaces an indent, numbers exactly as written', () => {
    const big = '123456789012345678901234567890';
    expect(formatJson(`{"a":[1,${big}],"b":{},"c":[],"d":{"e":"x, y: z"}}`)).toBe(
      `{\n  "a": [\n    1,\n    ${big}\n  ],\n  "b": {},\n  "c": [],\n  "d": {\n    "e": "x, y: z"\n  }\n}\n`,
    );
  });

  it('declines what is not one JSON value', () => {
    expect(formatJson('{"a": 1}\n{"a": 2}')).toBeNull();
    expect(formatJson('{"a": 1')).toBeNull();
    expect(formatJson('{"a": nope}')).toBeNull();
    expect(formatJson('"open')).toBeNull();
    expect(formatJson('   ')).toBeNull();
  });
});
