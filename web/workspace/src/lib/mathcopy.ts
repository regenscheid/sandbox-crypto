// SPDX-License-Identifier: MIT
/** Copy original math from the shared renderer, including display-only repairs (LD-44). */

const FORMULA = '.math[data-math-source]';
const ORDINARY_COPY = 'input, textarea, [contenteditable]:not([contenteditable="false"]), ' +
  'pre, .math-mark, .math-details';

function elementAt(node: Node): Element | null {
  return node.nodeType === 1 ? node as Element : node.parentElement;
}

function formulaAt(node: Node, root: HTMLElement): Element | null {
  const element = elementAt(node);
  if (element?.closest(ORDINARY_COPY)) return null;
  const math = element?.closest(FORMULA) ?? null;
  return math && root.contains(math) && math.closest('.markdown') ? math : null;
}

/** Keep Markdown block boundaries; unlike textContent, adjacent displayed formulas
 * and paragraphs must not run together. Existing text-node whitespace stays exact. */
function plainText(node: Node): string {
  const parts: (string | null)[] = [];
  const blocks = new Set(['P', 'DIV', 'PRE', 'LI', 'BLOCKQUOTE', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'TR']);
  const visit = (current: Node): void => {
    if (current.nodeType === 3) {
      parts.push(current.textContent ?? '');
      return;
    }
    const element = current.nodeType === 1 ? current as Element : null;
    if (element?.hasAttribute('hidden')) return;
    if (element?.tagName === 'DETAILS' && !element.hasAttribute('open')) {
      // Living-document front matter is collapsed by default. Copying nearby math
      // must not expose the closed YAML body as if it had been selected visibly.
      const summary = [...current.childNodes].find(
        (child) => child.nodeType === 1 && (child as Element).tagName === 'SUMMARY',
      );
      if (summary) visit(summary);
      return;
    }
    if (element?.tagName === 'BR') {
      parts.push('\n');
      return;
    }
    const block = element !== null && blocks.has(element.tagName);
    if (block) parts.push(null);
    const cells = element?.tagName === 'TR' ? [...current.childNodes].filter(
      (child) => child.nodeType === 1 && ['TD', 'TH'].includes((child as Element).tagName),
    ) : [];
    if (cells.length) {
      cells.forEach((cell, index) => {
        if (index > 0) parts.push('\t');
        visit(cell);
      });
    } else {
      for (const child of current.childNodes) visit(child);
    }
    if (block) parts.push(null);
  };
  visit(node);
  let text = '';
  let boundary = false;
  for (const part of parts) {
    if (part === null) boundary = true;
    else if (part) {
      if (boundary && text && !text.endsWith('\n') && !part.startsWith('\n')) text += '\n';
      text += part;
      boundary = false;
    }
  }
  return text;
}

/** A copy event changes only when its selection is inside this app and contains math.
 * Browser clipboard permissions and asynchronous clipboard reads are unnecessary. */
export function copyMath(event: ClipboardEvent, root: HTMLElement): void {
  if (event.defaultPrevented || !event.clipboardData) return;
  const selection = root.ownerDocument.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount !== 1) return;
  const selected = selection.getRangeAt(0);
  if (!root.contains(selected.startContainer) || !root.contains(selected.endContainer)) return;
  const active = root.ownerDocument.activeElement;
  if (active?.closest('input, textarea, [contenteditable]:not([contenteditable="false"])')) return;

  // Never move the live selection, even when only part of a formula was selected.
  const range = selected.cloneRange();
  const start = formulaAt(range.startContainer, root);
  const end = formulaAt(range.endContainer, root);
  if (start) range.setStartBefore(start);
  if (end) range.setEndAfter(end);
  const fragment = range.cloneContents();
  // A selection inside one Markdown view need not clone that view's outer element.
  // Across views, cloneContents retains each Markdown ancestor instead. Either way,
  // metadata elsewhere in the app must not acquire formula-copy behavior.
  const insideMarkdown = (elementAt(range.commonAncestorContainer)?.closest('.markdown') ?? null) !== null;
  const formulas = [...fragment.querySelectorAll(FORMULA)].filter(
    (math) => insideMarkdown || math.closest('.markdown') !== null,
  );
  if (formulas.length === 0) return;

  // Preserve normal rich HTML copying. Only plain text substitutes the exact source,
  // dropping display-only repair controls and hidden diagnostic text with the wrapper.
  const html = root.ownerDocument.createElement('div');
  html.append(fragment.cloneNode(true));
  for (const math of formulas) {
    const source = math.getAttribute('data-math-source');
    if (source !== null) math.replaceChildren(root.ownerDocument.createTextNode(source));
  }
  event.clipboardData.setData('text/html', html.innerHTML);
  event.clipboardData.setData('text/plain', plainText(fragment));
  event.preventDefault();
}

/** Install once for the mounted app; copy events can target the document's body. */
export function installMathCopy(root: HTMLElement): () => void {
  const listener = (event: ClipboardEvent): void => copyMath(event, root);
  root.ownerDocument.addEventListener('copy', listener);
  return () => root.ownerDocument.removeEventListener('copy', listener);
}
