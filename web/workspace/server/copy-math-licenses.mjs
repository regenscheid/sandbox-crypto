// SPDX-License-Identifier: MIT
import { fileURLToPath } from 'node:url';
import { verifyFigureDistribution, verifyMathDistribution } from './license-notices.mjs';

// Keep the established build entry point; Vite now emits all checked notices and
// the unchanged launchers together, and this final step refuses uncovered bytes: the
// math checker's, and the figure checker's beside it.
const root = fileURLToPath(new URL('..', import.meta.url));
verifyMathDistribution(root);
verifyFigureDistribution(root);
