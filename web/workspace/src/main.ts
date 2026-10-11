// SPDX-License-Identifier: MIT
import { mount } from 'svelte';
import 'katex/dist/katex.min.css';
import './app.css';
import App from './App.svelte';

const target = document.getElementById('app');
if (target === null) throw new Error('the page has no #app element');

export default mount(App, { target });
