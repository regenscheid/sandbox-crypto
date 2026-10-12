// SPDX-License-Identifier: MIT
/**
 * The macros a document's math renders with (living documents, LD-28): those of its front
 * matter above project definitions, as the server resolved them. Keys include the
 * trusted project/context identity so repair/render caches cannot cross scopes.
 *
 * KaTeX expands a macro wherever a formula uses it, and a few short macros that use one
 * another can expand into a great many characters: three of them took KaTeX 39 s when a
 * formula defined them itself, which is why a formula may not (mathrepair.ts). Macros
 * from the front matter are bounded the same way, on both sides:
 *
 * - at most `MAX_MACROS` of them, each a control sequence defined in at most `MAX_MACRO`
 *   characters;
 * - none whose definition defines another, as `\def` does, or uses one of its arguments
 *   more than once, which would let a macro multiply what it is given;
 * - and as KaTeX renders a formula, each expansion of one of them counts its definition's
 *   characters, and a formula whose macros expand to more than `MAX_EXPANDED` in all is
 *   not rendered, but shown as written with why.
 *
 * The server leaves out of the front matter's macros those that define others, use an
 * argument twice, expand into themselves, or expand to more than its own limit
 * (documents/frontmatter.py), and warns of each; the browser checks again what it is
 * given. A set of macros has a key (`MacroSet.key`), which the page's caches of KaTeX's
 * results and of model repairs are kept by, since a formula renders otherwise under
 * other macros.
 */
import { definesMacros } from './mathrepair';

export const MAX_MACROS = 200;
/** Characters in one macro's definition. */
export const MAX_MACRO = 1_000;
/** Characters of definitions one formula's macros may expand to, in all. */
export const MAX_EXPANDED = 2_000;

const NAME = /^\\(?:[A-Za-z]+|[^A-Za-z\s])$/u;

/** Thrown, while KaTeX renders, by a formula whose macros expand too far. */
export class TooMuchExpansion extends Error {
  constructor() {
    super(`its macros expand to more than ${MAX_EXPANDED} characters`);
    this.name = 'TooMuchExpansion';
  }
}

/** A macro KaTeX calls as it expands it; it returns the definition. */
type KatexMacro = (context: object) => string;

export interface MacroSet {
  /** The set's key in the page's caches: the same definitions, the same key. */
  readonly key: string;
  /** Stable server cache scope; this is an identity, never source read authority. */
  readonly repairScope?: { project: string; macro_context: string };
  /** The macros used, by name. */
  readonly definitions: Readonly<Record<string, string>>;
  /** The macros left out, each with why. */
  readonly problems: readonly string[];
  /**
   * KaTeX's `macros` for one formula: each expansion counts its definition's characters,
   * and throws `TooMuchExpansion` once they pass `MAX_EXPANDED`.
   */
  forFormula(): Record<string, KatexMacro>;
}

/** The arguments a definition uses more than once, as `#1`; `##` is a `#`. */
function repeatedArgument(definition: string): string | null {
  const seen = new Set<string>();
  const plain = definition.replaceAll('##', '');
  for (const match of plain.matchAll(/#([1-9])/g)) {
    if (seen.has(match[1]!)) return `#${match[1]}`;
    seen.add(match[1]!);
  }
  return null;
}

/** The keys given so far, by the definitions they stand for. */
const keys = new Map<string, string>();

function keyOf(used: Record<string, string>, scope: string): string {
  const entries = Object.entries(used);
  if (entries.length === 0 && !scope) return '';
  const canonical = JSON.stringify([scope, entries.sort(([a], [b]) => (a < b ? -1 : 1))]);
  let key = keys.get(canonical);
  if (key === undefined) {
    key = `macros-${keys.size + 1}`;
    keys.set(canonical, key);
  }
  return key;
}

/** The macros of `definitions` that may be used, with a key for their set. */
export function macroSet(definitions: Readonly<Record<string, string>>, scope = ''): MacroSet {
  const problems: string[] = [];
  const used: Record<string, string> = {};
  let count = 0;
  for (const [name, definition] of Object.entries(definitions)) {
    if (count === MAX_MACROS) {
      problems.push(`only the first ${MAX_MACROS} macros are used`);
      break;
    }
    if (!NAME.test(name) || typeof definition !== 'string') {
      problems.push(`${name.slice(0, 40)} is not a macro's name and definition`);
      continue;
    }
    if (definition.length > MAX_MACRO) {
      problems.push(`${name} is longer than ${MAX_MACRO} characters`);
      continue;
    }
    const defines = definesMacros(definition);
    if (defines !== null) {
      problems.push(`${name} defines another (\\${defines})`);
      continue;
    }
    const repeated = repeatedArgument(definition);
    if (repeated !== null) {
      problems.push(`${name} uses ${repeated} more than once`);
      continue;
    }
    used[name] = definition;
    count += 1;
  }
  const scoped = /^([A-Za-z0-9][A-Za-z0-9._-]{0,127}):([A-Za-z0-9._:-]{1,256})$/u.exec(scope);
  return {
    key: keyOf(used, scope),
    ...(scoped ? { repairScope: { project: scoped[1]!, macro_context: scoped[2]! } } : {}),
    definitions: used,
    problems,
    forFormula() {
      let expanded = 0;
      const macros: Record<string, KatexMacro> = {};
      for (const [name, definition] of Object.entries(used)) {
        macros[name] = () => {
          expanded += definition.length;
          if (expanded > MAX_EXPANDED) throw new TooMuchExpansion();
          return definition;
        };
      }
      return macros;
    },
  };
}

/** No macros: text outside documents. */
export const NO_MACROS: MacroSet = macroSet({});
