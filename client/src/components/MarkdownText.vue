<!-- client/src/components/MarkdownText.vue -->
<!-- 安全渲染模型输出的 Markdown：用真实元素渲染，不用 v-html，模型即使输出 HTML 也不会被执行 -->
<template>
  <div class="md">
    <template v-for="(block, bi) in blocks" :key="bi">
      <component
        :is="'h' + Math.min(block.level + 2, 6)"
        v-if="block.type === 'heading'"
        class="md-heading"
      >
        <InlineText :tokens="block.tokens" @cite="$emit('cite', $event)" />
      </component>

      <hr v-else-if="block.type === 'hr'" class="md-hr" />

      <pre v-else-if="block.type === 'code'" class="md-code"><code>{{ block.text }}</code></pre>

      <blockquote v-else-if="block.type === 'quote'" class="md-quote">
        <InlineText :tokens="block.tokens" @cite="$emit('cite', $event)" />
      </blockquote>

      <ul v-else-if="block.type === 'list' && !block.ordered" class="md-list">
        <li v-for="(item, ii) in block.items" :key="ii">
          <InlineText :tokens="item" @cite="$emit('cite', $event)" />
        </li>
      </ul>

      <ol v-else-if="block.type === 'list'" class="md-list md-list-ordered">
        <li v-for="(item, ii) in block.items" :key="ii">
          <InlineText :tokens="item" @cite="$emit('cite', $event)" />
        </li>
      </ol>

      <div v-else-if="block.type === 'table'" class="md-table-wrap">
        <table class="md-table">
          <thead>
            <tr>
              <th v-for="(cell, ci) in block.header" :key="ci">
                <InlineText :tokens="cell" @cite="$emit('cite', $event)" />
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, ri) in block.rows" :key="ri">
              <td v-for="(cell, ci) in row" :key="ci">
                <InlineText :tokens="cell" @cite="$emit('cite', $event)" />
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p v-else class="md-p">
        <InlineText :tokens="block.tokens" @cite="$emit('cite', $event)" />
      </p>
    </template>
  </div>
</template>

<script setup>
import { computed } from 'vue';
import { parseMarkdown } from '../markdown.js';
import InlineText from './InlineText.vue';

const props = defineProps({
  content: { type: String, default: '' },
  // 知识库问答用：哪些 [n] 是真实存在的来源编号（会被渲染成可点引用）
  cites: { type: Array, default: () => [] },
});
defineEmits(['cite']);

const blocks = computed(() => parseMarkdown(props.content, props.cites));
</script>

<style scoped>
/* 父级气泡默认是 pre-wrap 的纯文本样式，这里改回正常文档流 */
.md { white-space: normal; word-break: break-word; min-width: 0; max-width: 100%; }

.md-p { margin: 0 0 6px; line-height: 1.75; }
.md-p:last-child { margin-bottom: 0; }

.md-heading {
  margin: 10px 0 6px; font-size: 13.5px; font-weight: 600; line-height: 1.5;
}
.md-heading:first-child { margin-top: 0; }

.md-list { margin: 4px 0 8px; padding-left: 20px; }
.md-list li { line-height: 1.75; margin: 2px 0; }
.md-list-ordered { list-style: decimal; }
.md-list:last-child { margin-bottom: 0; }

.md-table-wrap { overflow-x: auto; margin: 8px 0; }
.md-table { border-collapse: collapse; font-size: 12.5px; min-width: 60%; }
.md-table th, .md-table td {
  border: 1px solid rgba(148, 163, 184, .5);
  padding: 5px 10px; text-align: left; white-space: nowrap;
}
.md-table th { background: rgba(148, 163, 184, .16); font-weight: 600; }

.md-quote {
  margin: 6px 0; padding: 4px 10px;
  border-left: 3px solid rgba(148, 163, 184, .6);
  color: inherit; opacity: .85;
}

.md-hr { border: none; border-top: 1px solid rgba(148, 163, 184, .45); margin: 10px 0; }

.md-code {
  margin: 8px 0; padding: 10px 12px; border-radius: 8px;
  background: rgba(15, 23, 42, .06); overflow-x: auto;
  font-size: 12px; line-height: 1.6;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
</style>
