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
 * 1. the deterministic repairs (`repairTex`, texRepairs.ts), chosen by KaTeX's error: commands KaTeX
 *    lacks, labels and references, unbalanced braces and `\left`/`\right`, bare `%`, `#`
 *    and `&`, double and dangling scripts, alignment outside an environment, display-only
 *    environments in inline math, and unknown commands as operator names;
 * 2. the reviewer model's repair, when the server offers one. It is asked for once per
 *    expression, in the background, and the page remembers the answer (`ModelRepairs`,
 *    in modelRepairs.ts, which only the browser loads);
 *    the text is rendered again when it arrives;
 * 3. a plain-text approximation in Unicode (`texToUnicode`, texUnicode.ts).
 *
 * Whatever is shown is marked with how it was repaired, and the expression as written is
 * kept beside it, to read and copy. The text itself never changes.
 */
import type { MathRepairRequest } from './api';
import type { ModelRepairs } from './modelRepairs';
import { Kept } from './kept';
import { safeRepair } from './texDefinitions';
import { repairTex } from './texRepairs';
import { texToUnicode } from './texUnicode';
import { visible } from './text';

/** The longest error message kept, from KaTeX or the server. */
const MAX_ERROR_LENGTH = 300;

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
