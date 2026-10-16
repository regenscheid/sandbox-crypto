// SPDX-License-Identifier: MIT
/**
 * A framed page's size, for the figure frame and the viewer's figure mode (FG-14, FG-17).
 * A frame of another site, in a page the browser is not showing (a background tab, a
 * hidden pane), may have no width until it is shown: such a page waits for a width
 * before it draws, then draws anyway at its own size after a while, and tells the
 * workspace its size again whenever it changes.
 */

/** How long a page waits for a width before it draws at its own size. */
export const LAYOUT_FALLBACK_MILLISECONDS = 1500;
/** The least time between two sizes a page sends. */
export const RESIZE_MILLISECONDS = 200;

/** The width of `element` once it has one, or 0 after `fallback` milliseconds. */
export function laidOut(
  element: Element,
  fallback = LAYOUT_FALLBACK_MILLISECONDS,
): Promise<number> {
  const width = () => element.getBoundingClientRect().width;
  if (width() > 0 || typeof ResizeObserver === 'undefined') return Promise.resolve(width());
  return new Promise((resolve) => {
    const observer = new ResizeObserver(() => {
      if (width() > 0) done();
    });
    const timer = setTimeout(done, fallback);
    function done(): void {
      clearTimeout(timer);
      observer.disconnect();
      resolve(width());
    }
    observer.observe(element);
  });
}

/**
 * Call `changed` whenever the size `measure` gives changes, as `elements` resize: at
 * once, then at most once every `every` milliseconds, the latest size last. Returns what
 * stops it.
 */
export function watchSize(
  elements: readonly Element[],
  measure: () => { width: number; height: number },
  changed: (width: number, height: number) => void,
  every = RESIZE_MILLISECONDS,
): () => void {
  if (typeof ResizeObserver === 'undefined') return () => {};
  let last = measure();
  let waiting: ReturnType<typeof setTimeout> | undefined;
  let due = false;
  const send = () => {
    const size = measure();
    if (size.width === last.width && size.height === last.height) return;
    last = size;
    changed(size.width, size.height);
    waiting = setTimeout(() => {
      waiting = undefined;
      if (due) {
        due = false;
        send();
      }
    }, every);
  };
  const observer = new ResizeObserver(() => {
    if (waiting === undefined) send();
    else due = true;
  });
  for (const element of elements) observer.observe(element);
  return () => {
    observer.disconnect();
    clearTimeout(waiting);
  };
}
