// SPDX-License-Identifier: MIT
/**
 * The workspace's one renderer for text: Markdown with LaTeX math (D25, D65).
 *
 * Everything shown here was written by an agent, a user or a paper, so it is untrusted
 * (threat model, T6). markdown-it runs with `html: false`, which escapes raw HTML; links
 * are kept only for absolute http and https addresses and mailto, and open in a new tab
 * without a referrer; images become links and are never loaded. Math goes to KaTeX in
 * its untrusted mode (`trust: false`: no `\href`, `\url`, `\includegraphics` or
 * `\html…`), with limits on sizes and macro expansion, and no macros shared between
 * formulas. With `throwOnError: false`, a formula KaTeX cannot parse comes back as its
 * source; it is shown as written, beside KaTeX's message. A text sends at most
 * `MAX_FORMULAS` formulas and `MAX_MATH` characters of math to KaTeX, and a formula at
 * most `MAX_FORMULA`; beyond that, math is shown as written. Rendering is a view: the
 * text is never changed, and KaTeX's MathML keeps each formula's source for copying.
 *
 * KaTeX caps only positive sizes, so a formula can still draw outside its own box (with
 * `\smash` or a negative `\raisebox`); the stylesheet clips each rendered text to its
 * own box, so that it cannot cover what the page shows beside it.
 */
import markdownit from 'markdown-it';
import type { Env, MarkdownIt, Token } from 'markdown-it';
import katex from 'katex';
import type { KatexOptions } from 'katex';
import { dollarMath, type DollarMathOptions } from './dollarmath';
import { visible } from './text';

/** The dollar math rules; the server's mdit-py-plugins must use the same settings. */
export const MATH_RULES: DollarMathOptions = {
  allowLabels: true,
  allowSpace: false,
  allowBlankLines: false,
  doubleInline: true,
};

export const KATEX_OPTIONS = {
  throwOnError: false,
  trust: false,
  strict: 'ignore',
  maxSize: 20,
  maxExpand: 1000,
  output: 'htmlAndMathml',
} as const satisfies KatexOptions;

/** Longer text is shown as written: rendering it would hold up the page. */
export const MAX_RENDERED = 200_000;
/** The formulas one text renders, and the characters of math in all of them. */
export const MAX_FORMULAS = 1_000;
export const MAX_MATH = 20_000;
/** The longest formula rendered; KaTeX's output grows about 150 times its input. */
export const MAX_FORMULA = 5_000;

const LINK = /^(?:https?:\/\/|mailto:)/i;
const OPENS_APART = 'target="_blank" rel="noopener noreferrer nofollow"';
/** KaTeX's rendering of source it could not parse; its title holds the message, escaped. */
const KATEX_ERROR = /^<span class="katex-error" title="([^"]*)"/;

/** What one text has sent to KaTeX so far, kept in its rendering's environment. */
interface MathBudget {
  formulas: number;
  characters: number;
}

const BUDGET = Symbol('math budget');

function budgetOf(env: Env | undefined): MathBudget {
  let budget = env?.[BUDGET] as MathBudget | undefined;
  if (budget === undefined) {
    // renderMarkdown always gives an environment, so a budget lasts for a whole text.
    budget = { formulas: 0, characters: 0 };
    if (env !== undefined) env[BUDGET] = budget;
  }
  return budget;
}

const md = markdownit('default', { html: false, linkify: false, typographer: false });
/** The configured parser, for tests of where math begins and ends. */
export const parser: MarkdownIt = md;
const escapeHtml = md.utils.escapeHtml;
md.validateLink = (url: string): boolean => LINK.test(url.trim());
dollarMath(md, MATH_RULES);
// Code shows bidirectional controls as code points, as tool calls do (text.ts).
md.core.ruler.push('visible_controls', (state) => {
  for (const token of state.tokens) {
    if (token.type === 'fence' || token.type === 'code_block') token.content = visible(token.content);
    for (const child of token.children ?? []) {
      if (child.type === 'code_inline') child.content = visible(child.content);
    }
  }
});

md.renderer.rules.link_open = (tokens, index, options, _env, self) => {
  const token = tokens[index]!;
  token.attrSet('target', '_blank');
  token.attrSet('rel', 'noopener noreferrer nofollow');
  return self.renderToken(tokens, index, options);
};
md.renderer.rules.image = (tokens, index, options, env, self) => {
  const token = tokens[index]!;
  const source = String(token.attrGet('src') ?? '');
  const alt = self.renderInlineAsText(token.children ?? [], options, env);
  return `<a class="image-link" href="${escapeHtml(source)}" ${OPENS_APART}>image: ${escapeHtml(alt || source)}</a>`;
};
md.renderer.rules.math_inline = (tokens, index, _options, env) =>
  renderMath(tokens[index]!, 'span', false, budgetOf(env));
md.renderer.rules.math_inline_double = (tokens, index, _options, env) =>
  renderMath(tokens[index]!, 'span', true, budgetOf(env));
md.renderer.rules.math_block = (tokens, index, _options, env) =>
  renderMath(tokens[index]!, 'div', true, budgetOf(env));
md.renderer.rules.math_block_label = (tokens, index, _options, env) =>
  renderMath(tokens[index]!, 'div', true, budgetOf(env));

function renderMath(
  token: Token,
  tag: 'span' | 'div',
  display: boolean,
  budget: MathBudget,
): string {
  const written = `${token.markup}${token.content}${token.markup}`;
  budget.formulas += 1;
  budget.characters += token.content.length;
  if (token.content.length > MAX_FORMULA) {
    return asWritten(tag, written, 'math-skipped', 'not rendered: too long a formula');
  }
  if (budget.formulas > MAX_FORMULAS || budget.characters > MAX_MATH) {
    return asWritten(tag, written, 'math-skipped', 'not rendered: this text has too much math');
  }
  let html: string;
  try {
    html = katex.renderToString(token.content.trim(), { ...KATEX_OPTIONS, displayMode: display });
  } catch (error) {
    // Only errors other than parse errors are thrown with throwOnError: false.
    return asWritten(tag, written, 'math-error', escapeHtml(String(error)));
  }
  const failed = KATEX_ERROR.exec(html);
  if (failed) {
    return asWritten(tag, written, 'math-error', failed[1]!.replace(/^ParseError: /, ''));
  }
  const label = token.info ? `<span class="math-label">(${escapeHtml(token.info)})</span>` : '';
  const kind = display ? 'math-display' : 'math-inline';
  return `<${tag} class="math ${kind}">${html}${label}</${tag}>`;
}

/** Math shown as written, with why it is not rendered, side by side; `message` is HTML. */
function asWritten(tag: 'span' | 'div', written: string, kind: string, message: string): string {
  return (
    `<${tag} class="math ${kind}"><code class="math-source">${escapeHtml(written)}</code>` +
    `<span class="math-message">${message}</span></${tag}>`
  );
}

/** Markdown with math as HTML that is safe to insert. */
export function renderMarkdown(source: string): string {
  if (source.length > MAX_RENDERED) {
    return (
      `<p class="note">This text has ${source.length} characters, more than are rendered, ` +
      `so it is shown as written.</p><pre>${escapeHtml(source)}</pre>`
    );
  }
  return md.render(source, {});
}
