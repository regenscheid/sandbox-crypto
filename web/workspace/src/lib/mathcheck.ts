// SPDX-License-Identifier: MIT
/** Mechanical renderer check. It makes no model/network calls and returns no HTML. */
import katex from 'katex';
import { macroSet } from './macros';
import { KATEX_OPTIONS, MAX_FORMULA } from './mathoptions';
import { describeError } from './mathErrors';
import { definesMacros } from './texDefinitions';
import { repairTex } from './texRepairs';

export interface Formula {
  id: string;
  tex: string;
  display: boolean;
  macros: Readonly<Record<string, string>>;
}

export function checkFormula(formula: Formula) {
  const common = {
    protocol: 1,
    id: formula.id,
    engine: katex.version,
    options: KATEX_OPTIONS,
    max_formula: MAX_FORMULA,
  };
  // A size skip is inconclusive, as in the GUI. It is never accepted math.
  if (formula.tex.length > MAX_FORMULA) {
    return {
      ...common,
      status: 'unavailable',
      error: 'formula exceeds renderer limit',
      remedy: null,
    };
  }
  const tex = formula.tex.trim();
  const definition = definesMacros(tex);
  if (definition !== null) {
    return { ...common, status: 'unavailable', error: 'formula defines a macro', remedy: null };
  }
  const macros = macroSet(formula.macros);
  const render = (candidate: string) =>
    katex.renderToString(candidate, {
      ...KATEX_OPTIONS,
      displayMode: formula.display,
      macros: macros.forFormula(),
    });
  try {
    render(tex);
    return { ...common, status: 'accepted', error: null, remedy: null };
  } catch (failure) {
    // Budget/macro failures are skips in the GUI. Only ParseError proves rejection.
    if (!(failure instanceof katex.ParseError)) {
      return { ...common, status: 'unavailable', error: describeError(failure), remedy: null };
    }
    const error = describeError(failure);
    for (const candidate of repairTex(tex, error, formula.display)) {
      if (candidate.length > MAX_FORMULA || definesMacros(candidate) !== null) continue;
      try {
        render(candidate);
        return { ...common, status: 'rejected', error, remedy: candidate };
      } catch {
        /* A repair must pass the same renderer. */
      }
    }
    return { ...common, status: 'rejected', error, remedy: null };
  }
}
