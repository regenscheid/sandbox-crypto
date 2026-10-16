// SPDX-License-Identifier: MIT
import type { Plugin } from 'vite';
import type { OutputBundle } from 'rolldown';

/** Collect deterministic checked notice files for an emitted production bundle. */
export function collectNotices(
  root: string,
  bundle: OutputBundle,
  distribution?: 'gui' | 'math' | 'viewer' | 'figures' | 'figure-check',
  sourceRoot?: string,
): Record<string, string>;

/** Include checked runtime and asset notices in the production GUI distribution. */
export function licenseNotices(distribution?: 'gui' | 'math' | 'viewer' | 'figures' | 'figure-check', gui?: string): Plugin;

/** The viewer build's directories: the GUI's, the page's and the output's. */
export const VIEWER: { gui: string; root: string; outDir: string };

/** Copy the PDF.js files the viewer loads at run time, and only those. */
export function pdfjsAssets(gui: string): Plugin;

/** Verify every emitted standalone math file against its checked distribution manifest. */
export function verifyMathDistribution(root: string): void;

/** Verify the figure checker's emitted files, beside the math checker's. */
export function verifyFigureDistribution(root: string): void;
