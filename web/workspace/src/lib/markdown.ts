// SPDX-License-Identifier: MIT
/**
 * The workspace's one renderer for text: Markdown with LaTeX math (D25, D65).
 *
 * Everything shown here was written by an agent, a user or a paper, so it is untrusted
 * (threat model, T6). markdown-it runs with `html: false`, which escapes raw HTML; links
 * are kept only for absolute http and https addresses and mailto, and open in a new tab
 * without a referrer; images become links and are never loaded. Math is written with
 * dollars (dollarmath.ts) or LaTeX's own delimiters (mathdelimiters.ts), and goes to
 * KaTeX in its untrusted mode (`trust: false`: no `\href`, `\url`, `\includegraphics`
 * or `\html…`), with limits on sizes and macro expansion, and no macros shared between
 * formulas. A formula KaTeX cannot parse goes to the repairs (mathrepair.ts): what is
 * shown then is marked with how it was repaired, beside the formula as written. A text
 * sends at most `MAX_FORMULAS` formulas and `MAX_MATH` characters of math to KaTeX, and
 * a formula at most `MAX_FORMULA`; beyond that, math is shown as written. Its repairs
 * send at most `MAX_REPAIR_MATH` characters more. Rendering is a view: the text is never
 * changed.
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
import { backslashMath, type MathMeta } from './mathdelimiters';
import {
  recoveredHtml,
  recoverMath,
  repairKey,
  type MathRenderer,
  type ModelRepairs,
} from './mathrepair';
import { visible } from './text';

/** The dollar math rules; the server's mdit-py-plugins must use the same settings. */
export const MATH_RULES: DollarMathOptions = {
  allowLabels: true,
  allowSpace: false,
  allowBlankLines: false,
  doubleInline: true,
};

/** KaTeX's settings. A parse error throws, and goes to the repairs. */
export const KATEX_OPTIONS = {
  throwOnError: true,
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
/** The characters of repaired math one text may send to KaTeX besides. */
export const MAX_REPAIR_MATH = 20_000;

const LINK = /^(?:https?:\/\/|mailto:)/i;
const OPENS_APART = 'target="_blank" rel="noopener noreferrer nofollow"';

/** What one text has sent to KaTeX so far. */
interface MathBudget {
  formulas: number;
  characters: number;
  /** Characters of repaired math. */
  repairs: number;
}

/** One text's rendering, kept in its environment: its budget, and the repairs it uses. */
interface Rendering {
  budget: MathBudget;
  repairs: ModelRepairs | null;
  /** The keys of the model repairs the text shows as pending. */
  waiting: Set<string>;
}

const RENDERING = Symbol('rendering');

function newRendering(repairs: ModelRepairs | null): Rendering {
  return { budget: { formulas: 0, characters: 0, repairs: 0 }, repairs, waiting: new Set() };
}

function renderingOf(env: Env | undefined): Rendering {
  let rendering = env?.[RENDERING] as Rendering | undefined;
  if (rendering === undefined) {
    // renderMarkdown always gives an environment, so a rendering lasts for a whole text.
    rendering = newRendering(null);
    if (env !== undefined) env[RENDERING] = rendering;
  }
  return rendering;
}

const md = markdownit('default', { html: false, linkify: false, typographer: false });
/** The configured parser, for tests of where math begins and ends. */
export const parser: MarkdownIt = md;
const escapeHtml = md.utils.escapeHtml;
md.validateLink = (url: string): boolean => LINK.test(url.trim());
dollarMath(md, MATH_RULES);
backslashMath(md);
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
  renderMath(tokens[index]!, 'span', false, renderingOf(env));
md.renderer.rules.math_inline_double = (tokens, index, _options, env) =>
  renderMath(tokens[index]!, 'span', true, renderingOf(env));
md.renderer.rules.math_block = (tokens, index, _options, env) =>
  renderMath(tokens[index]!, 'div', true, renderingOf(env));
md.renderer.rules.math_block_label = (tokens, index, _options, env) =>
  renderMath(tokens[index]!, 'div', true, renderingOf(env));

/** KaTeX for the repairs, within the text's budget for them. */
function budgeted(budget: MathBudget): MathRenderer {
  return {
    renderToString(tex, options) {
      if (tex.length > MAX_FORMULA || budget.repairs + tex.length > MAX_REPAIR_MATH) {
        throw new Error('beyond the budget for repairs');
      }
      budget.repairs += tex.length;
      return katex.renderToString(tex, options as KatexOptions);
    },
  };
}

/** KaTeX for checking a model's repair when it arrives, outside any text's rendering. */
const checker: MathRenderer = {
  renderToString(tex, options) {
    if (tex.length > MAX_FORMULA) throw new Error('the repair is too long to render');
    return katex.renderToString(tex, options as KatexOptions);
  },
};

function renderMath(
  token: Token,
  tag: 'span' | 'div',
  display: boolean,
  rendering: Rendering,
): string {
  const close = (token.meta as MathMeta | null)?.close ?? token.markup;
  const written = `${token.markup}${token.content}${close}`;
  const budget = rendering.budget;
  budget.formulas += 1;
  budget.characters += token.content.length;
  if (token.content.length > MAX_FORMULA) {
    return asWritten(tag, written, 'math-skipped', 'not rendered: too long a formula');
  }
  if (budget.formulas > MAX_FORMULAS || budget.characters > MAX_MATH) {
    return asWritten(tag, written, 'math-skipped', 'not rendered: this text has too much math');
  }
  const label = token.info ? `<span class="math-label">(${escapeHtml(token.info)})</span>` : '';
  const tex = token.content.trim();
  let html: string;
  try {
    html = katex.renderToString(tex, { ...KATEX_OPTIONS, displayMode: display });
  } catch (error) {
    if (!(error instanceof katex.ParseError)) {
      return asWritten(tag, written, 'math-error', escapeHtml(String(error)));
    }
    const recovery = recoverMath(budgeted(budget), tex, {
      displayMode: display,
      error,
      settings: KATEX_OPTIONS,
      repairs: rendering.repairs,
      check: checker,
    });
    if (recovery.status === 'fallback' && recovery.state === 'pending') {
      rendering.waiting.add(repairKey(tex, display));
    }
    return recoveredHtml(recovery, tag, display, written, label);
  }
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

/** A text rendered: HTML that is safe to insert, and the model repairs it waits for. */
export interface Rendered {
  html: string;
  /** Keys (mathrepair.ts, `repairKey`) of the formulas shown until a repair arrives. */
  waiting: ReadonlySet<string>;
}

/** Markdown with math as HTML, asking `repairs` for the formulas nothing else repairs. */
export function renderMarkdown(source: string, repairs: ModelRepairs | null = null): Rendered {
  if (source.length > MAX_RENDERED) {
    const html =
      `<p class="note">This text has ${source.length} characters, more than are rendered, ` +
      `so it is shown as written.</p><pre>${escapeHtml(source)}</pre>`;
    return { html, waiting: new Set() };
  }
  const rendering = newRendering(repairs);
  const html = md.render(source, { [RENDERING]: rendering });
  return { html, waiting: rendering.waiting };
}
