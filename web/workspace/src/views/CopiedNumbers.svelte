<!-- SPDX-License-Identifier: MIT -->
<!-- A copied-number finding's two numbers, on request (docs/specs/consistency-review.md,
     CR-39): lining them up costs as much as their digits multiplied, so the card does it
     only once the reader opens this. -->
<script lang="ts">
  import CopiedDigits from './CopiedDigits.svelte';

  let { stated, held }: { stated: string; held: string } = $props();
  let open = $state(false);
</script>

<details class="copied" ontoggle={(event) => (open = event.currentTarget.open)}>
  <summary>Compare the two numbers digit by digit</summary>
  {#if open}<CopiedDigits {stated} {held} />{/if}
</details>
