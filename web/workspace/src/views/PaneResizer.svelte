<!-- SPDX-License-Identifier: MIT -->
<script lang="ts">
  let { width = $bindable(460) }: { width?: number } = $props();
  let start: { x: number; width: number } | null = null;
  const clamp = (value: number) => Math.min(760, Math.max(320, value));
</script>

<!-- A focusable separator is the ARIA window-splitter pattern; arrows adjust its value. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  class="pane-resizer"
  role="separator"
  tabindex="0"
  aria-label="Resize research pane"
  aria-orientation="vertical"
  aria-valuemin="320"
  aria-valuemax="760"
  aria-valuenow={width}
  onkeydown={(event) => {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      width = clamp(
        event.key === 'Home'
          ? 320
          : event.key === 'End'
            ? 760
            : width + (event.key === 'ArrowLeft' ? 24 : -24),
      );
    }
  }}
  onpointerdown={(event) => {
    if (event.button !== 0) return;
    start = { x: event.clientX, width };
    event.currentTarget.setPointerCapture(event.pointerId);
  }}
  onpointermove={(event) => {
    if (start) width = clamp(start.width + start.x - event.clientX);
  }}
  onpointerup={() => {
    start = null;
  }}
  onpointercancel={() => {
    start = null;
  }}
  onlostpointercapture={() => {
    start = null;
  }}
></div>
