// SPDX-License-Identifier: MIT
/** Focus containment for compact navigation and research drawers. Last opened wins. */
const panels: HTMLElement[] = [];
function visible(element: HTMLElement): boolean {
  for (let current: HTMLElement | null = element; current; current = current.parentElement) {
    const style = getComputedStyle(current);
    if (
      current.hidden ||
      current.inert ||
      style.display === 'none' ||
      style.visibility === 'hidden'
    )
      return false;
    if (current instanceof HTMLDetailsElement && !current.open) {
      const summary = current.querySelector('summary');
      if (!summary?.contains(element)) return false;
    }
  }
  return true;
}
export function focusPanel(node: HTMLElement, options: { active: boolean; close: () => void }) {
  let active = false;
  let previous: HTMLElement | null = null;
  const ownsFocus = () => active && panels.findLast(visible) === node;
  const elements = () =>
    [
      ...node.querySelectorAll<HTMLElement>(
        'a[href], button:not(:disabled), input:not(:disabled):not([type="hidden"]), select:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]',
      ),
    ].filter((element) => element.tabIndex >= 0 && visible(element));
  const focusFirst = () => (elements()[0] ?? node).focus();
  function update(next: typeof options) {
    options = next;
    if (next.active === active) return;
    active = next.active;
    if (active) {
      previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      panels.push(node);
      queueMicrotask(() => {
        if (ownsFocus()) focusFirst();
      });
    } else {
      panels.splice(panels.indexOf(node), 1);
      // Svelte removes the background's inert attribute in the same update.
      queueMicrotask(() => {
        if (!active) restore();
      });
    }
  }
  function restore() {
    if (previous?.isConnected && visible(previous)) previous.focus();
    const owner = panels.findLast(visible);
    if (owner && !owner.contains(document.activeElement)) {
      const first = [
        ...owner.querySelectorAll<HTMLElement>(
          'button, a[href], input, select, textarea, summary, [tabindex="0"]',
        ),
      ].find(
        (element) => element.tabIndex >= 0 && !element.matches(':disabled') && visible(element),
      );
      (first ?? owner).focus();
    }
  }
  function focus(event: FocusEvent) {
    if (ownsFocus() && event.target instanceof Node && !node.contains(event.target)) focusFirst();
  }
  function key(event: KeyboardEvent) {
    if (!ownsFocus()) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      options.close();
    }
    if (event.key !== 'Tab') return;
    const items = elements(),
      first = items[0],
      last = items.at(-1);
    if (!first || (event.shiftKey && document.activeElement === first)) {
      event.preventDefault();
      (last ?? node).focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
  node.addEventListener('keydown', key);
  document.addEventListener('focusin', focus);
  update(options);
  return {
    update,
    destroy() {
      node.removeEventListener('keydown', key);
      document.removeEventListener('focusin', focus);
      if (active) {
        panels.splice(panels.indexOf(node), 1);
        restore();
      }
    },
  };
}
