<!-- SPDX-License-Identifier: MIT -->
<!-- A small line icon, drawn in the text's color. -->
<script lang="ts" module>
  const PATHS = {
    overview: 'M2.5 2.5h4.5v4.5h-4.5z M9 2.5h4.5v4.5H9z M2.5 9h4.5v4.5h-4.5z M9 9h4.5v4.5H9z',
    plan: 'M3 3.5h2 M7 3.5h6 M3 8h2 M7 8h6 M3 12.5h2 M7 12.5h6',
    document: 'M4 1.5h5.5L13 5v9.5H4z M9.5 1.5V5H13',
    artifact: 'M2.5 4.5h11v9h-11z M5 2.5h6',
    ledger: 'M3 3.5h10 M3 8h10 M3 12.5h10',
    memory:
      'M8 2.5c3 0 5.5 1 5.5 2.5S11 7.5 8 7.5 2.5 6.5 2.5 5 5 2.5 8 2.5z M2.5 5v6c0 1.5 2.5 2.5 5.5 2.5s5.5-1 5.5-2.5V5 M2.5 8c0 1.5 2.5 2.5 5.5 2.5s5.5-1 5.5-2.5',
    skill: 'M8 2l1.8 3.8 4.2.6-3 2.9.7 4.2L8 11.5l-3.7 2 .7-4.2-3-2.9 4.2-.6z',
    connector:
      'M6 10l4-4 M4.5 8.5l-1.5 1.5a2.1 2.1 0 003 3l1.5-1.5 M11.5 7.5l1.5-1.5a2.1 2.1 0 00-3-3L8.5 4.5',
    settings:
      'M8 5.5a2.5 2.5 0 100 5 2.5 2.5 0 000-5z M8 1.5v2 M8 12.5v2 M1.5 8h2 M12.5 8h2 M3.4 3.4l1.4 1.4 M11.2 11.2l1.4 1.4 M3.4 12.6l1.4-1.4 M11.2 4.8l1.4-1.4',
    projects: 'M2 4.5h4l1.5 1.5H14v7H2z',
    plus: 'M8 3v10 M3 8h10',
    chevron: 'M6 3.5l4.5 4.5L6 12.5',
    down: 'M4.5 6.5l3.5 3.5 3.5-3.5',
    send: 'M8 13V3 M3.5 7.5L8 3l4.5 4.5',
    check: 'M3 8.5l3.2 3L13 4.5',
    sidebar: 'M2 3h12v10H2z M6 3v10',
    logout: 'M6 2.5H3v11h3 M10 5l3 3-3 3 M13 8H6',
  } as const;
  export type IconName = keyof typeof PATHS;
</script>

<script lang="ts">
  let {
    name,
    size = 16,
    class: extra = 'nav-icon',
  }: { name: IconName; size?: number; class?: string } = $props();
</script>

<svg
  class="icon {extra}"
  width={size}
  height={size}
  viewBox="0 0 16 16"
  fill="none"
  stroke="currentColor"
  stroke-width="1.5"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"><path d={PATHS[name]} /></svg
>
