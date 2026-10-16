// SPDX-License-Identifier: MIT
/**
 * Figure files on the preview origin (docs/specs/figures.md, FG-12, FG-13). A PNG or a
 * JPEG is shown in an `<img>` whose bytes come from the preview origin, for a capability
 * the workspace mints; the workspace's own origin draws no figure. Any other type, or an
 * image the preview origin refuses (its bytes are not its type, or it is too large), is a
 * link to the artifact.
 */
import type { ArtifactEntry } from './api-types';
import { viewerOrigin } from './pdfAnchors';

/** The types a page shows as an image (FG-12). */
export const IMAGE_TYPES: ReadonlySet<string> = new Set(['image/png', 'image/jpeg']);
/** The largest image a page is given (FG-13); the preview origin checks it again. */
export const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

/** Whether an artifact revision is shown as an image, with the preview origin on. */
export function shownAsImage(
  entry: Pick<ArtifactEntry, 'media_type' | 'size' | 'deleted' | 'content_retained'>,
  previewOn: boolean,
): boolean {
  return (
    previewOn &&
    !entry.deleted &&
    entry.content_retained &&
    IMAGE_TYPES.has(entry.media_type) &&
    entry.size <= MAX_IMAGE_BYTES
  );
}

/** The types a page shows in a frame (FG-12, FG-14): an SVG in the figure frame, an HTML
 * file in a frame without scripts, and a PDF in the viewer's figure mode. */
export const FRAMED_TYPES: ReadonlySet<string> = new Set([
  'image/svg+xml',
  'text/html',
  'application/pdf',
]);

/** Whether an artifact revision is shown in a figure frame, with the preview origin on. */
export function shownInFrame(
  entry: Pick<ArtifactEntry, 'media_type' | 'size' | 'deleted' | 'content_retained'>,
  previewOn: boolean,
): boolean {
  return (
    previewOn &&
    !entry.deleted &&
    entry.content_retained &&
    FRAMED_TYPES.has(entry.media_type) &&
    entry.size <= MAX_IMAGE_BYTES
  );
}

/** The limits a frame draws within (FG-17): at most this many frames draw at once, and a
 * figure not drawn within this many milliseconds is abandoned. */
export const DRAWING_AT_ONCE = 8;
export const DRAW_MILLISECONDS = 5000;
/** The height a frame takes, in CSS pixels, at most; an HTML figure's, fixed. */
export const MAX_FRAME_HEIGHT = 4000;
export const HTML_FRAME_HEIGHT = 400;

/** Turns to draw: at most `limit` hold one at once; the others wait in order. */
export class Slots {
  private held = 0;
  private readonly waiting: (() => void)[] = [];

  constructor(private readonly limit: number) {}

  /** Wait for a turn; the function it gives back ends it, once. */
  async acquire(): Promise<() => void> {
    if (this.held >= this.limit) await new Promise<void>((resolve) => this.waiting.push(resolve));
    else this.held += 1;
    let done = false;
    return () => {
      if (done) return;
      done = true;
      const next = this.waiting.shift();
      if (next) next();
      else this.held -= 1;
    };
  }
}

/** The page's frames, drawing at most `DRAWING_AT_ONCE` at once (FG-17). */
export const drawing = new Slots(DRAWING_AT_ONCE);

/** An image's URL, if the server gave one on an http(s) origin; anything else is not
 * loaded. */
export function imageSource(url: string): string | null {
  return viewerOrigin(url) === null ? null : url;
}
