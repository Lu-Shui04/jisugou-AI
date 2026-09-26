<!-- client/src/views/AgentView.vue -->
<template>
  <div class="agent-page">
    <header class="chat-header">
      <div class="header-left">
        <div class="avatar">购</div>
        <div>
          <h1>极速购智能客服（Agent 模式）</h1>
          <span :class="['status', { active: !loading }]">
            {{ loading ? '思考中...' : '在线' }}
          </span>
        </div>
      </div>
      <button @click="clearMessages">清空对话</button>
    </header>

    <main class="messages-wrap" ref="messagesRef">
      <div v-if="messages.length === 0" class="welcome">
        <p>您好，我是极速购智能客服小购。</p>
        <p>我可以帮您查询订单状态和物流信息，直接说「我的订单」就行。</p>
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
          <div v-if="msg.steps && msg.steps.length" class="steps-wrap">
            <div v-for="(step, si) in msg.steps" :key="si" class="step-item">
              <span class="step-label">调用工具</span>
              <span class="step-tool">{{ step.tool }}</span>
              <span class="step-input">{{ formatInput(step.toolInput) }}</span>
            </div>
          </div>
          <div v-if="msg.thinking" class="thinking">
            <span class="dot-1">.</span>
            <span class="dot-2">.</span>
            <span class="dot-3">.</span>
          </div>
          <div v-else class="bubble">
            <!-- 模型输出带 Markdown（加粗/列表/表格）时按格式渲染，不再显示 ** 符号 -->
            <MarkdownText v-if="msg.role === 'assistant'" :content="msg.content" />
            <template v-else>{{ msg.content }}</template>
          </div>
        </div>
      </div>

      <!-- 当前轮次实时步骤 -->
      <div v-if="loading && steps.length" class="message-row assistant">
        <div class="avatar-sm">购</div>
        <div class="message-content">
          <div class="steps-wrap">
            <div v-for="(step, si) in steps" :key="si" class="step-item">
              <span class="step-label">调用工具</span>
              <span class="step-tool">{{ step.tool }}</span>
              <span class="step-input">{{ formatInput(step.toolInput) }}</span>
            </div>
          </div>
          <div class="thinking">
            <span class="dot-1">.</span>
            <span class="dot-2">.</span>
            <span class="dot-3">.</span>
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
        {{ loading ? '思考中...' : '发送' }}
      </button>
    </footer>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue';
import { useAgent } from '../composables/useAgent.js';
import MarkdownText from '../components/MarkdownText.vue';

const { messages, loading, steps, error, sendMessage, clearMessages } = useAgent();

const inputText   = ref('');
const messagesRef = ref(null);

// 预设词一律用"我的订单"（不写死订单号）：
// 权限系统上线后身份由服务端令牌认定，写死 ORD-001 只会让非本人看到"无权查看"
const quickQuestions = [
  '查一下我的订单',
  '我的订单到哪里了？',
  '我最近一笔订单是什么状态？',
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

const formatInput = (input) => {
  if (!input) return '';
  return Object.values(input).join(' · ');
};
</script>

<style scoped>
.agent-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  max-width: 780px;
  margin: 0 auto;
  background: #f8fafc;
  font-family: -apple-system, 'PingFang SC', sans-serif;
}

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  background: #fff;
  border-bottom: 1px solid #e2e8f0;
}
.header-left { display: flex; align-items: center; gap: 12px; }
.avatar {
  width: 42px; height: 42px; border-radius: 12px;
  background: #1d4ed8; color: #fff;
  font-size: 18px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.header-left h1 { font-size: 16px; font-weight: 600; margin: 0; color: #1e293b; }
.status { font-size: 12px; color: #94a3b8; }
.status.active { color: #22c55e; }

.messages-wrap {
  flex: 1; overflow-y: auto;
  padding: 20px 16px;
  display: flex; flex-direction: column; gap: 16px;
}

.welcome { text-align: center; padding: 40px 20px; color: #64748b; }
.welcome p { font-size: 15px; margin: 4px 0; }
.quick-btns { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 16px; }
.quick-btns button {
  padding: 7px 14px; border-radius: 20px;
  border: 1px solid #bfdbfe; background: #eff6ff;
  color: #2563eb; font-size: 13px; cursor: pointer;
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
.message-row.assistant .avatar-sm { background: #f1f5f9; color: #475569; }

.message-content { display: flex; flex-direction: column; gap: 6px; max-width: 75%; }
.message-row.user .message-content { align-items: flex-end; }

.bubble {
  padding: 12px 16px; border-radius: 16px;
  font-size: 14px; line-height: 1.7; white-space: pre-wrap;
}
.message-row.user .bubble {
  background: #2563eb; color: #fff;
  border-bottom-right-radius: 4px;
}
.message-row.assistant .bubble {
  background: #fff; color: #1e293b;
  border-bottom-left-radius: 4px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}

.steps-wrap { display: flex; flex-direction: column; gap: 4px; }
.step-item {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; background: #f1f5f9;
  border-radius: 6px; padding: 5px 10px; color: #475569;
}
.step-label { color: #94a3b8; }
.step-tool  { font-weight: 600; color: #2563eb; }
.step-input { color: #64748b; }

.thinking {
  display: flex; gap: 2px;
  padding: 10px 16px;
  background: #fff; border-radius: 12px;
  font-size: 22px; color: #94a3b8;
  width: fit-content;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}
.thinking span { animation: bounce 1.2s infinite; }
.dot-2 { animation-delay: .2s; }
.dot-3 { animation-delay: .4s; }
@keyframes bounce {
  0%, 80%, 100% { opacity: .3; transform: translateY(0); }
  40%           { opacity: 1;  transform: translateY(-4px); }
}

.error-tip {
  text-align: center; padding: 10px 16px;
  background: #fef2f2; color: #dc2626;
  border-radius: 8px; font-size: 13px;
}

.input-area {
  padding: 14px 16px; background: #fff;
  border-top: 1px solid #e2e8f0;
}
.input-area { display: flex; gap: 10px; align-items: flex-end; }
textarea {
  flex: 1; resize: none; border: 1px solid #e2e8f0;
  border-radius: 12px; padding: 10px 14px;
  font-size: 14px; font-family: inherit; line-height: 1.6;
  outline: none; background: #f8fafc;
  min-height: 42px; max-height: 120px;
}
textarea:focus   { border-color: #2563eb; background: #fff; }
textarea:disabled { opacity: 0.6; cursor: not-allowed; }

.send-btn {
  width: 80px; height: 42px; border-radius: 12px; flex-shrink: 0;
  border: none; background: #2563eb; color: #fff;
  font-size: 14px; font-weight: 600; cursor: pointer;
}
.send-btn:hover:not(:disabled) { background: #1d4ed8; }
.send-btn:disabled { background: #bfdbfe; cursor: not-allowed; }

/* ── 移动端适配（≤768px）：桌面端不受影响 ── */
@media (max-width: 768px) {
  .agent-page { width: 100%; max-width: 100%; margin: 0; }
  .chat-header { padding: 10px 12px; }
  .avatar { width: 36px; height: 36px; font-size: 16px; border-radius: 10px; }
  .header-left h1 { font-size: 15px; }
  .status { font-size: 11px; }
  .messages-wrap { padding: 12px 10px; gap: 12px; }
  .message-content { max-width: 88%; }
  .bubble { padding: 10px 13px; font-size: 14px; }
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
