// SPDX-License-Identifier: MIT
import { mount } from 'svelte';
import 'katex/dist/katex.min.css';
import './app.css';
import App from './App.svelte';
import { installMathCopy } from './lib/mathcopy';

const target = document.getElementById('app');
if (target === null) throw new Error('the page has no #app element');
installMathCopy(target);

export default mount(App, { target });
