// SPDX-License-Identifier: MIT
import { fileURLToPath } from 'node:url';
import { verifyMathDistribution } from './license-notices.mjs';

// Keep the established build entry point; Vite now emits all checked notices and
// the unchanged launcher together, and this final step refuses uncovered bytes.
verifyMathDistribution(fileURLToPath(new URL('..', import.meta.url)));
