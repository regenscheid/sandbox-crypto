// SPDX-License-Identifier: MIT
/**
 * The two numbers of a copied-number finding, side by side (docs/specs/consistency-
 * review.md, CR-39): the platform's check finds a long number a statement gives that
 * nearly matches one an artifact holds, and its statement names both. The view lines the
 * digits up, as few edits apart as they are, and marks the digits that differ.
 */

/** The two numbers a `contradicting_value` finding's statement names; null for another. */
export function copiedValues(finding: {
  category: string;
  statement: string;
}): { stated: string; held: string } | null {
  if (finding.category !== 'contradicting_value') return null;
  // Anchored at the end of the platform's text, so a number in the artifact's key, which an
  // agent may choose and the text quotes earlier, cannot stand in for the two named last.
  const found =
    /Stated: (\d{1,5000})\. In the artifact: (\d{1,5000})\. It looks copied by hand\.\s*$/.exec(
      finding.statement,
    );
  return found === null ? null : { stated: found[1]!, held: found[2]! };
}

/** One place of the two numbers lined up: a digit of each, or '' where one has none. */
export interface Column {
  stated: string;
  held: string;
}

/** The most digit pairs the view lines up, about 500 digits each; longer numbers are shown without marks. */
export const MAX_CELLS = 250_000;

/**
 * The digits of `stated` and `held` lined up as few edits apart as they are (an edit
 * distance's alignment): equal digits share a place, a changed digit faces the one it
 * replaced, and a digit one lacks faces ''. Null when the numbers are too long to line up.
 */
export function lineUp(stated: string, held: string): Column[] | null {
  const n = stated.length;
  const m = held.length;
  if ((n + 1) * (m + 1) > MAX_CELLS) return null;
  const width = m + 1;
  const cost = new Uint32Array((n + 1) * width);
  for (let i = 0; i <= n; i++) cost[i * width] = i;
  for (let j = 0; j <= m; j++) cost[j] = j;
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      const swap = cost[(i - 1) * width + j - 1]! + (stated[i - 1] === held[j - 1] ? 0 : 1);
      const drop = cost[(i - 1) * width + j]! + 1;
      const add = cost[i * width + j - 1]! + 1;
      cost[i * width + j] = Math.min(swap, drop, add);
    }
  }
  const columns: Column[] = [];
  let i = n;
  let j = m;
  while (i > 0 || j > 0) {
    const here = cost[i * width + j]!;
    if (
      i > 0 &&
      j > 0 &&
      here === cost[(i - 1) * width + j - 1]! + (stated[i - 1] === held[j - 1] ? 0 : 1)
    ) {
      columns.push({ stated: stated[--i]!, held: held[--j]! });
    } else if (i > 0 && here === cost[(i - 1) * width + j]! + 1) {
      columns.push({ stated: stated[--i]!, held: '' });
    } else {
      columns.push({ stated: '', held: held[--j]! });
    }
  }
  return columns.reverse();
}

/** The columns in rows of `width`, each with the place its first column has. */
export function digitRows(
  columns: readonly Column[],
  width = 40,
): { from: number; columns: Column[] }[] {
  const rows: { from: number; columns: Column[] }[] = [];
  for (let start = 0; start < columns.length; start += width) {
    rows.push({ from: start + 1, columns: columns.slice(start, start + width) });
  }
  return rows;
}
