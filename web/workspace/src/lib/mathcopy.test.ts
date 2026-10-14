// SPDX-License-Identifier: MIT
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { copyMath, installMathCopy } from './mathcopy';
import { forgetResults, renderDocument, renderMarkdown } from './markdown';
import { ModelRepairs, repairKey } from './mathrepair';
import { NO_MACROS } from './macros';

beforeEach(() => forgetResults());

describe('original copy source', () => {
  it.each([
    ['\\(  x  \\)', '\\(  x  \\)'],
    ['\\(x + y\\)', '\\(x + y\\)'],
    ['\\[ x = y \\]', '\\[ x = y \\]'],
    ['$$\nx + y\n$$', '$$\nx + y\n$$'],
    ['\\begin{align}x&=y\\end{align}', '\\begin{align}x&=y\\end{align}'],
    ['$\\bm{x}$', '$\\bm{x}$'],
    ['$\\frac{1$', '$\\frac{1$'],
    ["$$\\text{$& $1 $' $`}$$", "$$\\text{$& $1 $' $`}$$"],
  ])('retains the original delimiters and body of %s', (source, original) => {
    expect(renderMarkdown(source).html).toContain(
      `data-math-source="${original.replaceAll('&', '&amp;')}"`,
    );
  });

  it('keeps original model-repaired source and escapes attribute metacharacters', () => {
    const repairs = new ModelRepairs(null);
    repairs.set(repairKey('\\frac{1', false), { tex: '\\frac{1}{2}', model: 'synthetic' });
    const html = renderMarkdown('$\\frac{1$', repairs).html;
    expect(html).toContain('data-math-source="$\\frac{1$"');
    expect(html).toContain('data-repair="model"');
    expect(html).toContain('\\frac{1}{2}</annotation>');
    const hostile = '$\\text{"<&\u202E}$';
    const escaped = renderMarkdown(hostile).html;
    expect(escaped).toContain('data-math-source="$\\text{&quot;&lt;&amp;&#8238;}$"');
    expect(escaped).not.toContain('data-math-source="$\\text{"');
  });

  it('does not mark fenced or inline code as formulas', () => {
    expect(renderMarkdown('`$x$`\n\n```tex\n$y$\n```').html).not.toContain('data-math-source');
  });

  it.each(['> $$\n> x + y\n> $$', '- $$\n  x + y\n  $$'])(
    'copies the document formula without its quote/list structure',
    (source) => {
      const html = renderDocument(source, { blocks: [], macros: NO_MACROS }).html;
      expect(html).toContain('data-math-source="$$\nx + y\n$$"');
    },
  );
});

// These small DOM doubles test the copy boundary and text projection without adding a
// DOM dependency. Real Range/clipboard behavior is an explicit browser acceptance gate.
class CopyNode {
  parentElement: CopyNode | null = null;
  childNodes: CopyNode[] = [];
  hidden = false;
  open = false;
  constructor(
    readonly tagName = '',
    readonly value = '',
    readonly source: string | null = null,
  ) {}
  get nodeType(): number {
    return this.tagName ? 1 : this.value ? 3 : 11;
  }
  get textContent(): string {
    return this.value || this.childNodes.map((n) => n.textContent).join('');
  }
  get innerHTML(): string {
    return '<original selected HTML>';
  }
  append(...nodes: CopyNode[]): void {
    for (const node of nodes) {
      node.parentElement = this;
      this.childNodes.push(node);
    }
  }
  contains(node: CopyNode): boolean {
    return node === this || this.childNodes.some((c) => c.contains(node));
  }
  closest(selector: string): CopyNode | null {
    if (
      (selector === '.math[data-math-source]' && this.source !== null) ||
      (selector === '.markdown' && this.tagName === 'MARKDOWN') ||
      (selector.includes('input') && ['INPUT', 'TEXTAREA', 'PRE'].includes(this.tagName))
    )
      return this;
    return this.parentElement?.closest(selector) ?? null;
  }
  querySelectorAll(): CopyNode[] {
    return this.childNodes.flatMap((c) => (c.source === null ? c.querySelectorAll() : [c]));
  }
  getAttribute(): string | null {
    return this.source;
  }
  hasAttribute(name: string): boolean {
    return name === 'hidden' ? this.hidden : name === 'open' && this.open;
  }
  replaceChildren(node: CopyNode): void {
    this.childNodes = [];
    this.append(node);
  }
  cloneNode(): CopyNode {
    const cloned = new CopyNode(this.tagName, this.value, this.source);
    cloned.hidden = this.hidden;
    cloned.open = this.open;
    cloned.append(...this.childNodes.map((c) => c.cloneNode()));
    return cloned;
  }
}

function fixture(parts: CopyNode[]) {
  const root = new CopyNode('MARKDOWN');
  root.append(...parts);
  const fragment = new CopyNode();
  fragment.append(...parts.map((p) => p.cloneNode()));
  const cloned = {
    startContainer: parts[0]!.childNodes[0] ?? parts[0]!,
    endContainer: parts.at(-1)!.childNodes.at(-1) ?? parts.at(-1)!,
    commonAncestorContainer: root,
    setStartBefore: vi.fn(),
    setEndAfter: vi.fn(),
    cloneContents: () => fragment,
  };
  const selected = {
    ...cloned,
    setStartBefore: vi.fn(),
    setEndAfter: vi.fn(),
    cloneRange: vi.fn(() => cloned),
  };
  const selection = { isCollapsed: false, rangeCount: 1, getRangeAt: () => selected };
  const doc = {
    activeElement: null as CopyNode | null,
    getSelection: () => selection,
    createElement: () => new CopyNode('DIV'),
    createTextNode: (text: string) => new CopyNode('', text),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
  const app = Object.assign(root, { ownerDocument: doc });
  const event = {
    defaultPrevented: false,
    clipboardData: { setData: vi.fn() },
    preventDefault: vi.fn(),
  };
  const run = () => copyMath(event as unknown as ClipboardEvent, app as unknown as HTMLElement);
  return { app, doc, selected, cloned, selection, event, run };
}

function math(source: string, display = false): CopyNode {
  const node = new CopyNode(display ? 'DIV' : 'SPAN', '', source);
  node.append(new CopyNode('', 'rendered + repair mark + hidden diagnostic'));
  return node;
}

describe('copy boundary', () => {
  it('copies two original formulas with surrounding text, without repair UI', () => {
    const f = fixture([
      new CopyNode('', 'Before '),
      math('$\\bm{x}$'),
      new CopyNode('', ' and '),
      math('$\\frac{1$'),
      new CopyNode('', ' after'),
    ]);
    f.run();
    expect(f.event.clipboardData.setData).toHaveBeenCalledWith(
      'text/plain',
      'Before $\\bm{x}$ and $\\frac{1$ after',
    );
    expect(f.event.clipboardData.setData).toHaveBeenCalledWith(
      'text/html',
      '<original selected HTML>',
    );
    expect(f.event.preventDefault).toHaveBeenCalledOnce();
  });

  it('expands partial formula endpoints only on the cloned range', () => {
    const f = fixture([math('$a$'), math('$$b$$', true)]);
    f.run();
    expect(f.selected.cloneRange).toHaveBeenCalledOnce();
    expect(f.cloned.setStartBefore).toHaveBeenCalledWith(f.app.childNodes[0]);
    expect(f.cloned.setEndAfter).toHaveBeenCalledWith(f.app.childNodes[1]);
    expect(f.selected.setStartBefore).not.toHaveBeenCalled();
    expect(f.selected.setEndAfter).not.toHaveBeenCalled();
    expect(f.event.clipboardData.setData).toHaveBeenCalledWith('text/plain', '$a$\n$$b$$');
  });

  it('keeps display boundaries and exact internal source whitespace', () => {
    const f = fixture([math('$$\na\n$$', true), math('$$b$$', true)]);
    f.run();
    expect(f.event.clipboardData.setData).toHaveBeenCalledWith('text/plain', '$$\na\n$$\n$$b$$');
  });

  it('copies a visible as-written formula from its metadata, including original controls', () => {
    const written = math('$\u202Ex$');
    const code = new CopyNode('CODE');
    code.append(new CopyNode('', '$⟦U+202E⟧x$'));
    written.replaceChildren(code);
    const f = fixture([written]);
    f.run();
    expect(f.cloned.setStartBefore).toHaveBeenCalledWith(written);
    expect(f.cloned.setEndAfter).toHaveBeenCalledWith(written);
    expect(f.event.clipboardData.setData).toHaveBeenCalledWith('text/plain', '$\u202Ex$');
  });

  it('leaves ordinary code-only selection unchanged', () => {
    const code = new CopyNode('CODE');
    code.append(new CopyNode('', '$a$ code'));
    const f = fixture([code]);
    f.run();
    expect(f.event.preventDefault).not.toHaveBeenCalled();
  });

  it('keeps table cell tabs and row newlines in a mixed selection', () => {
    const table = new CopyNode('TABLE');
    for (const row of [
      ['Name', 'Formula', 'Next'],
      ['value', '$x$', 'last'],
    ]) {
      const tr = new CopyNode('TR');
      for (const text of row) {
        const cell = new CopyNode(row[0] === 'Name' ? 'TH' : 'TD');
        cell.append(text === '$x$' ? math(text) : new CopyNode('', text));
        tr.append(new CopyNode('', '\n'), cell);
      }
      tr.append(new CopyNode('', '\n'));
      table.append(tr);
    }
    const f = fixture([table]);
    f.run();
    expect(f.event.clipboardData.setData).toHaveBeenCalledWith(
      'text/plain',
      'Name\tFormula\tNext\nvalue\t$x$\tlast',
    );
  });

  it('does not transform arbitrary formula-shaped app metadata outside Markdown', () => {
    const f = fixture([math('$a$')]);
    f.cloned.commonAncestorContainer = new CopyNode('DIV');
    f.run();
    expect(f.event.preventDefault).not.toHaveBeenCalled();
  });

  it.each([false, true])('copies only visible front matter when details open=%s', (open) => {
    const details = new CopyNode('DETAILS');
    details.open = open;
    const summary = new CopyNode('SUMMARY');
    summary.append(new CopyNode('', 'Front matter'));
    const yaml = new CopyNode('PRE');
    yaml.append(new CopyNode('', 'macros: original-YAML'));
    details.append(summary, yaml);
    const hidden = new CopyNode('DIV');
    hidden.hidden = true;
    hidden.append(new CopyNode('', 'not-visible'));
    const f = fixture([details, new CopyNode('', '\n'), hidden, math('$x$')]);
    f.run();
    expect(f.event.clipboardData.setData).toHaveBeenCalledWith(
      'text/plain',
      open ? 'Front matter\nmacros: original-YAML\n$x$' : 'Front matter\n$x$',
    );
  });

  it.each(['empty', 'multiple', 'editor', 'outside', 'handled', 'unavailable', 'plain'])(
    'leaves %s copy to the browser',
    (kind) => {
      const f = fixture([kind === 'plain' ? new CopyNode('', 'ordinary source') : math('$a$')]);
      if (kind === 'empty') f.selection.isCollapsed = true;
      if (kind === 'multiple') f.selection.rangeCount = 2;
      if (kind === 'editor') f.doc.activeElement = new CopyNode('TEXTAREA');
      if (kind === 'outside') f.selected.startContainer = new CopyNode('', 'outside');
      if (kind === 'handled') f.event.defaultPrevented = true;
      if (kind === 'unavailable')
        f.event.clipboardData = null as unknown as typeof f.event.clipboardData;
      f.run();
      expect(f.event.preventDefault).not.toHaveBeenCalled();
    },
  );

  it('installs and removes the one app-scoped listener', () => {
    const f = fixture([math('$a$')]);
    const stop = installMathCopy(f.app as unknown as HTMLElement);
    const listener = f.doc.addEventListener.mock.calls[0]![1];
    expect(f.doc.addEventListener).toHaveBeenCalledWith('copy', listener);
    stop();
    expect(f.doc.removeEventListener).toHaveBeenCalledWith('copy', listener);
  });
});
