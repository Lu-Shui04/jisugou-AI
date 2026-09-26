<!-- client/src/views/GraphView.vue -->
<template>
  <div class="graph-page">
    <header class="chat-header">
      <div class="header-left">
        <div class="avatar">购</div>
        <div>
          <h1>极速购智能客服中枢</h1>
          <span class="subtitle">
            {{ loading ? currentNode || '处理中...' : '多 Agent 协作模式' }}
          </span>
        </div>
      </div>
      <button @click="clearMessages">清空</button>
    </header>

    <main class="messages-wrap" ref="messagesRef">
      <div v-if="messages.length === 0" class="welcome">
        <p>您好，我是极速购智能客服中枢。</p>
        <p>我会自动判断您的问题类型，调用最合适的模块为您服务。</p>
        <p>一句话说清就行，订单 + 商品 + 政策也能一次问完～</p>
        <div class="quick-btns">
          <button v-for="q in quickQuestions" :key="q" @click="handleQuick(q)">
            {{ q }}
          </button>
        </div>
      </div>

      <div
        v-for="(msg, i) in messages"
        :key="i"
        class="message-row"
        :class="msg.role"
      >
        <div class="avatar-sm">{{ msg.role === 'user' ? '我' : '购' }}</div>
        <div class="message-content">

          <!-- 工作流执行轨迹 -->
          <div v-if="msg.nodes && msg.nodes.length" class="flow-trace">
            <template v-for="(node, ni) in msg.nodes" :key="ni">
              <span class="node-name">{{ NODE_DISPLAY[node] || node }}</span>
              <span v-if="ni < msg.nodes.length - 1" class="arrow">→</span>
            </template>
            <span v-if="msg.intent" class="intent-tag">{{ msg.intent }}</span>
          </div>

          <!-- 工具调用步骤 -->
          <div v-if="msg.steps && msg.steps.length" class="steps-wrap">
            <div v-for="(step, si) in msg.steps" :key="si" class="step-item">
              <span class="step-tool">{{ step.tool }}</span>
              <span class="step-sep">·</span>
              <span class="step-input">{{ formatStepInput(step.input) }}</span>
            </div>
          </div>

          <!-- 加载中 -->
          <div v-if="msg.loading" class="bubble loading-bubble">
            <span class="dot" /><span class="dot" /><span class="dot" />
          </div>
          <!-- 回答 -->
          <div v-else class="bubble">
            <MarkdownText v-if="msg.role === 'assistant'" :content="msg.content" />
            <template v-else>{{ msg.content }}</template>
            <div v-if="msg.sources && msg.sources.length" class="source-hint">
              <span class="source-hint-label">📎 依据</span>
              <span
                v-for="item in msg.sources.slice(0, 3)"
                :key="item.index || item.source"
                class="source-hint-chip"
                :title="item.section || item.source"
              >{{ item.source }}<em v-if="item.score !== undefined"> · {{ Number(item.score).toFixed(2) }}</em></span>
            </div>
          </div>
        </div>
      </div>

      <div v-if="error" class="error-tip">{{ error }}</div>
    </main>

    <footer class="input-area">
      <textarea
        v-model="inputText"
        placeholder="输入消息，Enter 发送，Shift+Enter 换行"
        :disabled="loading"
        @keydown.enter.exact.prevent="handleSend"
        rows="1"
      />
      <button
        class="send-btn"
        :disabled="loading || !inputText.trim()"
        @click="handleSend"
      >
        {{ loading ? '处理中...' : '发送' }}
      </button>
    </footer>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue';
import { useGraph, NODE_LABELS } from '../composables/useGraph.js';
import MarkdownText from '../components/MarkdownText.vue';

const { messages, loading, currentNode, error, sendMessage, clearMessages } = useGraph();

const inputText   = ref('');
const messagesRef = ref(null);

const NODE_DISPLAY = NODE_LABELS;

// 预设词用"我的订单"：身份由服务端令牌认定，写死订单号会被判无权查看
const quickQuestions = [
  '查一下我的订单',
  '我的订单到哪里了？',
  '蓝牙耳机怎么保修？',
  '退款需要多少天？',
];

const scrollToBottom = async () => {
  await nextTick();
  if (messagesRef.value)
    messagesRef.value.scrollTop = messagesRef.value.scrollHeight;
};

const handleSend = async () => {
  const text = inputText.value.trim();
  if (!text || loading.value) return;
  inputText.value = '';
  await sendMessage(text, scrollToBottom);
};

const handleQuick = (q) => {
  inputText.value = q;
  handleSend();
};

const formatStepInput = (input) => {
  if (!input) return '';
  return Object.values(input).join(' · ');
};
</script>

<style scoped>
.graph-page {
  display: flex; flex-direction: column;
  height: 100vh; max-width: 780px;
  margin: 0 auto; background: #f8fafc;
  font-family: -apple-system, 'PingFang SC', sans-serif;
}
.chat-header {
  display: flex; align-items: center;
  justify-content: space-between;
  padding: 14px 20px; background: #fff;
  border-bottom: 1px solid #e2e8f0;
}
.header-left { display: flex; align-items: center; gap: 12px; }
.avatar {
  width: 42px; height: 42px; border-radius: 12px;
  background: #7c3aed; color: #fff;
  font-size: 18px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.header-left h1      { font-size: 16px; font-weight: 600; margin: 0; color: #1e293b; }
.header-left .subtitle { font-size: 12px; color: #94a3b8; transition: color .2s; }
.chat-header button {
  padding: 6px 14px; border-radius: 8px;
  border: 1px solid #e2e8f0; background: #fff;
  color: #64748b; cursor: pointer; font-size: 13px;
}

.messages-wrap {
  flex: 1; overflow-y: auto;
  padding: 20px 16px;
  display: flex; flex-direction: column; gap: 16px;
}
.welcome { text-align: center; padding: 40px 20px; color: #64748b; }

/* 知识库依据：回答了知识库里的内容，就把出处摆出来（用户能核对，也能判断是不是编的） */
.source-hint {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  margin-top: 8px; padding-top: 7px; border-top: 1px dashed rgba(148, 163, 184, .5);
  font-size: 11.5px; color: #475569;
}
.source-hint-label { color: #0f766e; font-weight: 600; }
.source-hint-chip {
  padding: 1px 7px; border-radius: 999px; background: #f0fdfa;
  border: 1px solid #99f6e4; color: #0f766e;
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.source-hint-chip em { font-style: normal; opacity: .75; }

.welcome p { font-size: 15px; margin: 0 0 8px; }
.quick-btns {
  display: flex; flex-wrap: wrap; gap: 8px;
  justify-content: center; margin-top: 16px;
}
.quick-btns button {
  padding: 7px 14px; border-radius: 20px;
  border: 1px solid #ddd6fe; background: #f5f3ff;
  color: #7c3aed; font-size: 13px; cursor: pointer;
}

.message-row { display: flex; gap: 8px; }
/* row-reverse 的主轴起点在右侧，贴右边的写法是 flex-start（头像落在最右） */
.message-row.user { flex-direction: row-reverse; justify-content: flex-start; }

.message-row.assistant { justify-content: flex-start; }

.avatar-sm {
  width: 32px; height: 32px; border-radius: 10px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700;
}
.message-row.user .avatar-sm      { background: #2563eb; color: #fff; }
.message-row.assistant .avatar-sm { background: #f5f3ff; color: #7c3aed; }

.message-content {
  display: flex; flex-direction: column; gap: 6px; max-width: 78%;
}
.message-row.user .message-content { align-items: flex-end; }

.flow-trace {
  display: flex; align-items: center;
  flex-wrap: wrap; gap: 4px; font-size: 11px;
}
.node-name {
  padding: 2px 8px; border-radius: 4px;
  background: #f5f3ff; color: #7c3aed;
  border: 1px solid #ddd6fe;
}
.arrow { color: #cbd5e1; font-size: 10px; }
.intent-tag {
  padding: 2px 8px; border-radius: 4px;
  background: #eff6ff; color: #2563eb;
  border: 1px solid #bfdbfe; margin-left: 4px;
}

.steps-wrap { display: flex; flex-direction: column; gap: 3px; }
.step-item {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; padding: 4px 10px;
  background: #f8fafc; border-radius: 6px;
  border: 1px solid #e2e8f0;
}
.step-tool  { font-weight: 600; color: #475569; }
.step-sep   { color: #cbd5e1; }
.step-input { color: #94a3b8; }

.bubble {
  padding: 12px 16px; border-radius: 16px;
  font-size: 14px; line-height: 1.7; white-space: pre-wrap;
}
.message-row.user .bubble {
  background: #2563eb; color: #fff; border-bottom-right-radius: 4px;
}
.message-row.assistant .bubble {
  background: #fff; color: #1e293b;
  border-bottom-left-radius: 4px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}

.loading-bubble {
  display: flex; gap: 5px; align-items: center;
  padding: 12px 16px; background: #fff; border-radius: 16px;
  border-bottom-left-radius: 4px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  min-width: 60px;
}
.dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: #94a3b8; animation: bounce 1.2s infinite;
}
.dot:nth-child(2) { animation-delay: .2s; }
.dot:nth-child(3) { animation-delay: .4s; }
@keyframes bounce {
  0%, 80%, 100% { transform: translateY(0); opacity: .4; }
  40%           { transform: translateY(-5px); opacity: 1; }
}

.error-tip {
  text-align: center; padding: 10px 16px;
  background: #fef2f2; color: #dc2626;
  border-radius: 8px; font-size: 13px;
}

.input-area {
  padding: 14px 16px; background: #fff;
  border-top: 1px solid #e2e8f0;
  display: flex; gap: 10px; align-items: flex-end;
}
textarea {
  flex: 1; resize: none; border: 1px solid #e2e8f0;
  border-radius: 12px; padding: 10px 14px;
  font-size: 14px; font-family: inherit;
  outline: none; background: #f8fafc;
  min-height: 42px; max-height: 120px;
}
textarea:focus    { border-color: #7c3aed; background: #fff; }
textarea:disabled { opacity: 0.6; }

.send-btn {
  width: 80px; height: 42px; border-radius: 12px;
  border: none; background: #7c3aed; color: #fff;
  font-size: 14px; font-weight: 600; cursor: pointer;
}
.send-btn:hover:not(:disabled) { background: #6d28d9; }
.send-btn:disabled { background: #ddd6fe; color: #7c3aed; cursor: not-allowed; }

/* ── 移动端适配（≤768px）：桌面端不受影响 ── */
@media (max-width: 768px) {
  .graph-page { width: 100%; max-width: 100%; margin: 0; }
  .chat-header { padding: 10px 12px; }
  .avatar { width: 36px; height: 36px; font-size: 16px; border-radius: 10px; }
  .header-left h1 { font-size: 15px; }
  .subtitle { font-size: 11px; }
  .messages-wrap { padding: 12px 10px; gap: 12px; }
  .message-content { max-width: 88%; }
  .bubble { padding: 10px 13px; font-size: 14px; }
  .flow-trace { font-size: 11px; }
  .welcome { padding: 24px 12px; }
  .input-area { padding: 10px 12px; }
  .send-btn { width: 64px; }

  /* 关键：flex 项默认 min-width:auto，宽表格会把气泡撑出屏幕（max-width 也压不住） */
  .message-row { min-width: 0; }
  .bubble-wrap, .message-content { min-width: 0; }
  /* 气泡自己也是 flex 项，要能收缩到比内容更窄，内部再横向滚动 */
  .bubble { overflow-wrap: anywhere; min-width: 0; max-width: 100%; }
  .md-table-wrap { max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .md-code { max-width: 100%; overflow-x: auto; }
}
</style>
