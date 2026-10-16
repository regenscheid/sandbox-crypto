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

/** An image's URL, if the server gave one on an http(s) origin; anything else is not
 * loaded. */
export function imageSource(url: string): string | null {
  return viewerOrigin(url) === null ? null : url;
}
