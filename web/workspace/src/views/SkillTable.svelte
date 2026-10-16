<!-- SPDX-License-Identifier: MIT -->
<!-- The skills a project's run is given (SK-24): each with its revision, whether it is an
     entry skill or loaded at every start, its description, and the short description and
     topic, marked by who set them: the import's pass (derived) or the user. -->
<script lang="ts">
  import type { SkillView } from '../lib/api-types';
  import { visible } from '../lib/text';

  let { skills, always }: { skills: SkillView[]; always: string[] } = $props();
  const by = (who: string | null) => (who === 'user' ? 'yours' : who ? 'derived' : null);
</script>

<table class="skills">
  <thead>
    <tr><th>Skill</th><th>Description</th><th>Topic</th></tr>
  </thead>
  <tbody>
    {#each skills as skill (skill.id)}
      <tr>
        <td>
          <code>{visible(skill.id)}</code>
          <span class="muted">@{skill.revision.slice(0, 12)}</span>
          {#if skill.entry}<span class="mark">entry</span>{/if}
          {#if always.includes(skill.id)}<span class="mark">always loaded</span>{/if}
        </td>
        <td>
          {visible(skill.description)}
          {#if skill.short}
            <br /><span class="muted">Short:</span>
            {visible(skill.short)}
            {#if by(skill.short_by)}<span class="mark">{by(skill.short_by)}</span>{/if}
          {/if}
        </td>
        <td>
          {#if skill.topic}
            {visible(skill.topic)}
            {#if by(skill.topic_by)}<span class="mark">{by(skill.topic_by)}</span>{/if}
          {/if}
        </td>
      </tr>
    {/each}
  </tbody>
</table>

<style>
  .skills td {
    vertical-align: top;
  }
  .mark {
    font-size: 0.8em;
    border: 1px solid currentColor;
    border-radius: 0.25rem;
    padding: 0 0.25rem;
    opacity: 0.75;
  }
</style>
