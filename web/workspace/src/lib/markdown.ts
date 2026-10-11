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
 * or `\html…`), with limits on sizes and macro expansion. A formula that defines a macro
 * is shown as written: macros come from the project (living documents, LD-28), and a
 * definition can multiply KaTeX's work far beyond the formula's length. A formula KaTeX
 * cannot parse goes to the repairs (mathrepair.ts): what is shown then is marked with how
 * it was repaired, beside the formula as written.
 *
 * A text sends at most `MAX_FORMULAS` formulas and `MAX_MATH` characters of math to
 * KaTeX, and a formula at most `MAX_FORMULA`; its repairs send at most `MAX_REPAIR_MATH`
 * characters more. KaTeX works at most `MAX_MATH_TIME` milliseconds in one go, a task of
 * the page's thread that every text rendered in it shares, as when a page first shows
 * all its texts; the math it had no time for is shown as written meanwhile, and rendered
 * in later goes (`whenMathTime`), each a task of its own. Without definitions, the worst
 * formula within these sizes takes KaTeX about 60 to 80 ms. The page keeps KaTeX's result
 * for each formula, so a text rendered again, as when a model repair arrives, sends KaTeX
 * only what is new. Rendering is a view: the text is never changed.
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
  definesMacros,
  Kept,
  OutOfTime,
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
/**
 * The milliseconds KaTeX may work in one go: a task of the page's thread, which every text
 * rendered in it shares. It stops before a formula, never within one, so a go can run
 * over by one formula's time.
 */
export const MAX_MATH_TIME = 250;

const LINK = /^(?:https?:\/\/|mailto:)/i;
const OPENS_APART = 'target="_blank" rel="noopener noreferrer nofollow"';

/** What one text has sent to KaTeX so far. */
interface MathBudget {
  formulas: number;
  characters: number;
  /** Characters of repaired math. */
  repairs: number;
}

/** KaTeX's result for one formula: its HTML, or what it threw. */
type Result = { html: string } | { error: unknown };

/**
 * KaTeX's results for one text's formulas, which its component keeps while some of its
 * math waits for time (`renderMarkdown`): a later go starts where the last one stopped,
 * whatever the page's cache has dropped since.
 */
export type TextResults = Map<string, Result>;

/**
 * KaTeX's results, for the page, by formula and display mode: KaTeX's settings are the
 * renderer's own, so a formula always renders alike. Within a count and a size. Once a
 * project's macros reach the renderer (living documents, LD-28), this key must also hold
 * the macros in force, and so must the keys of the repairs (`repairKey`), since KaTeX's
 * error and so the repairs depend on them.
 */
const results = new Kept<Result>(4_000, 8_000_000, (key, result) =>
  key.length + ('html' in result ? result.html.length : 200),
);

/** The milliseconds KaTeX has worked in this go. */
let spent = 0;
/** What waits for the next go: the texts with math they had no time for. */
const waitingForTime = new Set<{ run: () => void }>();
let renewal: ReturnType<typeof setTimeout> | null = null;

/** A new go, in a task after this one: KaTeX's time renewed, then the texts that wait. */
function renewLater(): void {
  if (renewal !== null) return;
  renewal = setTimeout(() => {
    renewal = null;
    spent = 0;
    const waiting = [...waitingForTime];
    waitingForTime.clear();
    for (const entry of waiting) entry.run();
  }, 0);
}

/**
 * Call `callback` once, in a later go, with KaTeX's time renewed: for a text with math
 * its last rendering had no time for. Returns a function that cancels it.
 */
export function whenMathTime(callback: () => void): () => void {
  const entry = { run: callback };
  waitingForTime.add(entry);
  renewLater();
  return () => {
    waitingForTime.delete(entry);
  };
}

/** Forget KaTeX's results and its time in this go; for tests. */
export function forgetResults(): void {
  results.clear();
  spent = 0;
}

/** A marker: KaTeX's time in this go is spent. */
const OUT_OF_TIME = Symbol('out of time');

/**
 * KaTeX's result for `tex`: the text's own from its last rendering, the page's, or worked
 * out now. A `limited` call works it out only while the go has time left; any call spends
 * the go's time.
 */
function resultFor(
  tex: string,
  display: boolean,
  rendering: Rendering | null,
  limited: boolean,
): Result | typeof OUT_OF_TIME {
  const key = repairKey(tex, display);
  let result = rendering?.earlier?.get(key) ?? results.get(key);
  if (result === undefined) {
    if (limited && spent >= MAX_MATH_TIME) return OUT_OF_TIME;
    const started = performance.now();
    try {
      result = { html: katex.renderToString(tex, { ...KATEX_OPTIONS, displayMode: display }) };
    } catch (error) {
      result = { error };
    }
    spent += performance.now() - started;
    renewLater();
    results.set(key, result);
  }
  rendering?.used.set(key, result);
  return result;
}

/** One text's rendering, kept in its environment: its budget, and the repairs it uses. */
interface Rendering {
  budget: MathBudget;
  repairs: ModelRepairs | null;
  /** The keys of the model repairs the text shows as pending. */
  waiting: Set<string>;
  /** KaTeX's results from the text's last rendering, when it left math for later. */
  earlier: TextResults | null;
  /** KaTeX's results in this rendering. */
  used: TextResults;
  /** Whether some of the text's math waits for a later go. */
  unfinished: boolean;
}

const RENDERING = Symbol('rendering');

function newRendering(repairs: ModelRepairs | null, earlier: TextResults | null): Rendering {
  return {
    budget: { formulas: 0, characters: 0, repairs: 0 },
    repairs,
    waiting: new Set(),
    earlier,
    used: new Map(),
    unfinished: false,
  };
}

function renderingOf(env: Env | undefined): Rendering {
  let rendering = env?.[RENDERING] as Rendering | undefined;
  if (rendering === undefined) {
    // renderMarkdown always gives an environment, so a rendering lasts for a whole text.
    rendering = newRendering(null, null);
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

/**
 * KaTeX for the repairs, with the renderer's settings, within the text's budget for
 * repaired math and the go's time (`OutOfTime` when it has none left).
 */
function budgeted(rendering: Rendering): MathRenderer {
  const budget = rendering.budget;
  return {
    renderToString(tex, options) {
      if (tex.length > MAX_FORMULA || budget.repairs + tex.length > MAX_REPAIR_MATH) {
        throw new Error('beyond the budget for repairs');
      }
      const result = resultFor(tex, options.displayMode === true, rendering, true);
      if (result === OUT_OF_TIME) throw new OutOfTime();
      budget.repairs += tex.length;
      if ('html' in result) return result.html;
      throw result.error;
    },
  };
}

/**
 * KaTeX for checking a model's repair when it arrives, outside any text's rendering: one
 * formula, checked whatever the go's time, since an answer it turned down for want of
 * time would be turned down for the page.
 */
const checker: MathRenderer = {
  renderToString(tex, options) {
    if (tex.length > MAX_FORMULA) throw new Error('the repair is too long to render');
    const result = resultFor(tex, options.displayMode === true, null, false);
    if (result === OUT_OF_TIME) throw new OutOfTime(); // not for an unlimited call
    if ('html' in result) return result.html;
    throw result.error;
  },
};

/** What a formula left for a later go shows meanwhile. */
const WAITING_FOR_TIME = 'not rendered yet: KaTeX renders a page’s math a part at a time';

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
  const tex = token.content.trim();
  const definition = definesMacros(tex);
  if (definition !== null) {
    const message = `not rendered: a formula may not define macros (\\${definition})`;
    return asWritten(tag, written, 'math-skipped', escapeHtml(message));
  }
  const result = resultFor(tex, display, rendering, true);
  if (result === OUT_OF_TIME) {
    rendering.unfinished = true;
    return asWritten(tag, written, 'math-skipped', WAITING_FOR_TIME);
  }
  const label = token.info ? `<span class="math-label">(${escapeHtml(token.info)})</span>` : '';
  if ('html' in result) {
    const kind = display ? 'math-display' : 'math-inline';
    return `<${tag} class="math ${kind}">${result.html}${label}</${tag}>`;
  }
  const error = result.error;
  if (!(error instanceof katex.ParseError)) {
    return asWritten(tag, written, 'math-error', escapeHtml(visible(String(error))));
  }
  const recovery = recoverMath(budgeted(rendering), tex, {
    displayMode: display,
    error,
    settings: KATEX_OPTIONS,
    repairs: rendering.repairs,
    check: checker,
  });
  if (recovery.status === 'unfinished') {
    rendering.unfinished = true;
    return asWritten(tag, written, 'math-skipped', WAITING_FOR_TIME);
  }
  if (recovery.status === 'fallback' && recovery.state === 'pending') {
    rendering.waiting.add(repairKey(tex, display));
  }
  return recoveredHtml(recovery, tag, display, written, label);
}

/** Math shown as written, with why it is not rendered, side by side; `message` is HTML. */
function asWritten(tag: 'span' | 'div', written: string, kind: string, message: string): string {
  return (
    `<${tag} class="math ${kind}"><code class="math-source">${escapeHtml(visible(written))}</code>` +
    `<span class="math-message">${message}</span></${tag}>`
  );
}

/** A text rendered: HTML that is safe to insert, and what it waits for. */
export interface Rendered {
  html: string;
  /** Keys (mathrepair.ts, `repairKey`) of the formulas shown until a repair arrives. */
  waiting: ReadonlySet<string>;
  /** Whether some of its math waits for a later go (`whenMathTime`). */
  unfinished: boolean;
}

/**
 * Markdown with math as HTML, asking `repairs` for the formulas nothing else repairs.
 * `earlier` is the text's own record of KaTeX's results: while the rendering leaves math
 * for a later go, it holds this rendering's results, for the next; otherwise it is
 * emptied.
 */
export function renderMarkdown(
  source: string,
  repairs: ModelRepairs | null = null,
  earlier: TextResults | null = null,
): Rendered {
  if (source.length > MAX_RENDERED) {
    const html =
      `<p class="note">This text has ${source.length} characters, more than are rendered, ` +
      `so it is shown as written.</p><pre>${escapeHtml(source)}</pre>`;
    earlier?.clear();
    return { html, waiting: new Set(), unfinished: false };
  }
  const rendering = newRendering(repairs, earlier);
  const html = md.render(source, { [RENDERING]: rendering });
  if (earlier !== null) {
    earlier.clear();
    if (rendering.unfinished) for (const [key, result] of rendering.used) earlier.set(key, result);
  }
  return { html, waiting: rendering.waiting, unfinished: rendering.unfinished };
}
