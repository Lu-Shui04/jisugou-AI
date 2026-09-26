<!-- client/src/components/InlineText.vue -->
<template>
  <template v-for="(token, index) in tokens" :key="index">
    <strong v-if="token.type === 'bold'">{{ token.text }}</strong>
    <code v-else-if="token.type === 'code'" class="md-inline-code">{{ token.text }}</code>
    <!-- 人工客服电话：手机点一下直接拨号 -->
    <a v-else-if="token.type === 'link'" class="md-link" :href="token.href">{{ token.text }}</a>
    <button
      v-else-if="token.type === 'cite'"
      class="md-cite"
      :title="'查看第 ' + token.index + ' 条来源'"
      @click="$emit('cite', token.index)"
    >{{ token.index }}</button>
    <template v-else>{{ token.text }}</template>
  </template>
</template>

<script setup>
defineProps({ tokens: { type: Array, default: () => [] } });
defineEmits(['cite']);
</script>

<style scoped>
strong { font-weight: 600; }
.md-inline-code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: .92em; padding: 1px 5px; border-radius: 4px;
  background: rgba(15, 23, 42, .07);
}
.md-link {
  color: #1d4ed8; font-weight: 700;
  text-decoration: none; border-bottom: 1px dashed currentColor;
}
.md-link:hover { color: #1e40af; }
.md-cite {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 18px; height: 18px; padding: 0 5px; margin: 0 2px;
  vertical-align: 1px; border-radius: 6px; border: 1px solid #99f6e4;
  background: #f0fdfa; color: #0f766e; font-size: 11px; font-weight: 700;
  font-family: inherit; cursor: pointer; transition: all .15s;
}
.md-cite:hover { background: #0f766e; border-color: #0f766e; color: #fff; }
</style>
