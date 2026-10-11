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
 * characters more. Texts render in goes of at most `MAX_GO_TIME` milliseconds, each a task
 * of the page's thread, as when a page first shows all its texts: a text that comes after
 * a go's time is spent waits for a later go (`whenMathTime`), and so does the math a text
 * had no time for, shown as written meanwhile. Without definitions, the worst formula
 * within these sizes takes KaTeX about 60 to 80 ms. The page keeps KaTeX's result for
 * each formula, so a text rendered again, as when a model repair arrives, sends KaTeX only
 * what is new. Rendering is a view: the text is never changed.
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
 * The milliseconds one go may take: a task of the page's thread in which texts render, one
 * after another, Markdown, KaTeX and the page's update all counted. Once they are spent,
 * no further text starts and KaTeX takes no further formula; but the first text and the
 * first formula of a go always render, so that each go gets further. A go can so run over
 * by one text's Markdown and one formula.
 */
export const MAX_GO_TIME = 250;

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

/** What a text's component keeps from one rendering to the next (`renderMarkdown`). */
export interface TextState {
  /**
   * KaTeX's results for the text's formulas, while some of its math waits for a go: the
   * next go starts where the last one stopped, whatever the page's cache has dropped.
   */
  results: Map<string, Result>;
  /** Its last rendering, and the source it is of: shown again while the text waits. */
  last: { source: string; rendered: Rendered } | null;
}

export function textState(): TextState {
  return { results: new Map(), last: null };
}

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

/** One go: when it began, and what it has rendered. */
interface Go {
  started: number;
  texts: number;
  formulas: number;
}

/** The go of this task, from its first rendering on; the next task begins another. */
let go: Go | null = null;
let goTimer: ReturnType<typeof setTimeout> | null = null;
/** The texts waiting for a go, in the order they asked. */
const waitingForTime = new Set<{ run: () => void }>();

function scheduleNextGo(): void {
  if (goTimer === null) goTimer = setTimeout(nextGo, 0);
}

/** The go this task renders in, begun by its first rendering; it ends with the task. */
function currentGo(): Go {
  if (go === null) {
    go = { started: performance.now(), texts: 0, formulas: 0 };
    scheduleNextGo();
  }
  return go;
}

function timeLeft(current: Go): boolean {
  return performance.now() - current.started < MAX_GO_TIME;
}

/** A new go: the texts that wait render one at a time while it has time; the rest wait on. */
function nextGo(): void {
  goTimer = null;
  go = null;
  if (waitingForTime.size === 0) return;
  const current = currentGo();
  for (const entry of [...waitingForTime]) {
    if (current.texts > 0 && !timeLeft(current)) break;
    if (waitingForTime.delete(entry)) entry.run();
  }
}

/**
 * Call `callback` once, in its turn in a later go, for a text that waits: it is to render
 * the text then, at once, so that the go counts the time. Returns a function that cancels
 * it.
 */
export function whenMathTime(callback: () => void): () => void {
  const entry = { run: callback };
  waitingForTime.add(entry);
  scheduleNextGo();
  return () => {
    waitingForTime.delete(entry);
  };
}

/** Forget KaTeX's results, and begin a new go; for tests. */
export function forgetResults(): void {
  results.clear();
  go = null;
}

/** A marker: this go's time is spent. */
const OUT_OF_TIME = Symbol('out of time');

/**
 * KaTeX's result for `tex`: the text's own from its last rendering, the page's, or worked
 * out now. A `limited` call works one out only while the go has time left, or when it is
 * the go's first.
 */
function resultFor(
  tex: string,
  display: boolean,
  rendering: Rendering | null,
  limited: boolean,
): Result | typeof OUT_OF_TIME {
  const key = repairKey(tex, display);
  let result = rendering?.state?.results.get(key) ?? results.get(key);
  if (result === undefined) {
    const current = currentGo();
    if (limited && current.formulas > 0 && !timeLeft(current)) return OUT_OF_TIME;
    try {
      result = { html: katex.renderToString(tex, { ...KATEX_OPTIONS, displayMode: display }) };
    } catch (error) {
      result = { error };
    }
    current.formulas += 1;
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
  /** What the text's component keeps, with KaTeX's results from its last rendering. */
  state: TextState | null;
  /** KaTeX's results in this rendering. */
  used: Map<string, Result>;
  /** Whether some of the text's math waits for a later go. */
  unfinished: boolean;
}

const RENDERING = Symbol('rendering');

function newRendering(repairs: ModelRepairs | null, state: TextState | null): Rendering {
  return {
    budget: { formulas: 0, characters: 0, repairs: 0 },
    repairs,
    waiting: new Set(),
    state,
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
  /** Whether the text, or some of its math, waits for a later go (`whenMathTime`). */
  unfinished: boolean;
}

/** A text no go has had time for yet: shown as written. */
function waitingHtml(source: string): string {
  return `<pre class="markdown-waiting">${escapeHtml(source)}</pre>`;
}

/**
 * Markdown with math as HTML, asking `repairs` for the formulas nothing else repairs.
 * With the text's `state`, a text that comes after this go's time is spent is not
 * rendered: it shows its last rendering again (or itself, as written, if it has none) and
 * waits for a later go; and a rendering that leaves math for later keeps KaTeX's results
 * for the next.
 */
export function renderMarkdown(
  source: string,
  repairs: ModelRepairs | null = null,
  state: TextState | null = null,
): Rendered {
  let rendered: Rendered;
  if (source.length > MAX_RENDERED) {
    const html =
      `<p class="note">This text has ${source.length} characters, more than are rendered, ` +
      `so it is shown as written.</p><pre>${escapeHtml(source)}</pre>`;
    rendered = { html, waiting: new Set(), unfinished: false };
  } else {
    const current = currentGo();
    if (state !== null && current.texts > 0 && !timeLeft(current)) {
      const last = state.last?.source === source ? state.last.rendered : null;
      return {
        html: last?.html ?? waitingHtml(source),
        waiting: last?.waiting ?? new Set(),
        unfinished: true,
      };
    }
    current.texts += 1;
    const rendering = newRendering(repairs, state);
    const html = md.render(source, { [RENDERING]: rendering });
    rendered = { html, waiting: rendering.waiting, unfinished: rendering.unfinished };
    if (state !== null && rendering.unfinished) {
      state.results = rendering.used;
      state.last = { source, rendered };
      return rendered;
    }
  }
  if (state !== null) {
    state.results = new Map();
    state.last = { source, rendered };
  }
  return rendered;
}
