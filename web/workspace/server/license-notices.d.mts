// SPDX-License-Identifier: MIT
import type { Plugin } from 'vite';
import type { OutputBundle } from 'rolldown';

/** Collect deterministic checked notice files for an emitted production bundle. */
export function collectNotices(root: string, bundle: OutputBundle): Record<string, string>;

/** Include checked runtime and asset notices in the production GUI distribution. */
export function licenseNotices(): Plugin;
