<!--
  第一章：Vue3 对话视图
  功能：
  - 展示完整对话历史
  - 流式输出实时展示（逐字出现效果）
  - 发送按钮 + Enter 键发送
  - 自动滚动到底部
  - 错误提示
-->
<template>
  <div class="chat-page">
    <!-- 顶部 Header -->
    <header class="chat-header">
      <div class="header-left">
        <div class="avatar">购</div>
        <div class="header-info">
          <h1>极速购智能客服</h1>
          <span class="status" :class="{ active: !streaming }">
            {{ streaming ? '回复中...' : '在线 · 陪你聊，也能帮你找到正确入口' }}
          </span>
        </div>
      </div>
      <button class="clear-btn" @click="clearMessages" title="清空对话">
        清空
      </button>
    </header>

    <!-- 消息列表 -->
    <main class="messages-wrap" ref="messagesRef">
      <!-- 欢迎消息 -->
      <div v-if="messages.length === 0" class="welcome">
        <div class="welcome-icon">👋</div>
        <p>您好！我是极速购智能客服小购</p>
        <p class="sub">我负责陪您聊天、介绍平台，并帮您找到正确的功能入口。</p>
        <p class="sub">查数据的事由下面几个页面来做，点一下就能过去～</p>

        <!-- 能力导航：把"指路"做成可点击的动作 -->
        <div class="guide-cards">
          <router-link
            v-for="item in guideCards"
            :key="item.route"
            :to="item.route"
            class="guide-card"
          >
            <span class="guide-card-title">{{ item.title }}</span>
            <span class="guide-card-desc">{{ item.desc }}</span>
          </router-link>
        </div>

        <div class="quick-questions">
          <button
            v-for="q in quickQuestions"
            :key="q"
            @click="handleQuickQuestion(q)"
          >
            {{ q }}
          </button>
        </div>
      </div>

      <!-- 历史消息 -->
      <div
        v-for="(msg, index) in messages"
        :key="index"
        class="message-row"
        :class="msg.role"
      >
        <div class="bubble-wrap">
          <div class="avatar-sm">
            {{ msg.role === 'user' ? '我' : '购' }}
          </div>
          <div class="bubble">
            <!-- 模型输出带 Markdown（加粗/列表/表格）时按格式渲染，不再显示 ** 符号 -->
            <MarkdownText v-if="msg.role === 'assistant'" :content="msg.content" />
            <p v-else>{{ msg.content }}</p>
            <div v-if="msg.sources && msg.sources.length" class="source-hint">
              <span class="source-hint-label">📎 依据</span>
              <span
                v-for="item in msg.sources.slice(0, 3)"
                :key="item.index || item.source"
                class="source-hint-chip"
                :title="item.section || item.source"
              >{{ item.source }}<em v-if="item.score !== undefined"> · {{ Number(item.score).toFixed(2) }}</em></span>
            </div>
            <!-- 模型引导时给出的跳转按钮 -->
            <div v-if="msg.links && msg.links.length" class="guide-links">
              <router-link
                v-for="link in msg.links"
                :key="link.route"
                :to="link.route"
                class="guide-link"
              >
                {{ link.label }} →
              </router-link>
            </div>
          </div>
        </div>
      </div>

      <!-- 流式输出中的消息 -->
      <div v-if="streaming" class="message-row assistant">
        <div class="bubble-wrap">
          <div class="avatar-sm">购</div>
          <div class="bubble streaming">
            <p>{{ streamText || '&nbsp;' }}<span class="cursor">▋</span></p>
          </div>
        </div>
      </div>

      <!-- 错误提示 -->
      <div v-if="error" class="error-tip">
        ⚠️ {{ error }}
      </div>
    </main>

    <!-- 输入区 -->
    <footer class="input-area">
      <div class="input-wrap">
        <textarea
          v-model="inputText"
          ref="inputRef"
          placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          :disabled="streaming"
          @keydown.enter.exact.prevent="handleSend"
          rows="1"
          @input="autoResize"
        />
        <button
          class="send-btn"
          :class="{ loading: streaming }"
          :disabled="streaming || !inputText.trim()"
          @click="handleSend"
        >
          <span v-if="!streaming">发送</span>
          <span v-else class="dot-loading">
            <i /><i /><i />
          </span>
        </button>
      </div>
    </footer>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue';
import { useChat } from '../composables/useChat.js';
import MarkdownText from '../components/MarkdownText.vue';

const { messages, streaming, streamText, error, sendMessage, clearMessages } =
  useChat();

// 欢迎页的能力导航（与页面右上角导航对应）
const guideCards = [
  { route: '/agent', title: '订单查询', desc: '订单状态 · 物流轨迹 · 退款进度' },
  { route: '/rag',   title: '知识库',   desc: '商品参数 · 保修 · 发票 · 退换货' },
  { route: '/graph', title: '智能中枢', desc: '一句话多个问题，自动分派处理' },
];

const inputText = ref('');
const messagesRef = ref(null);
const inputRef = ref(null);

const quickQuestions = [
  '你们都能做什么？',
  '我想查订单怎么办？',
  '商品和售后问题去哪问？',
  '人工客服电话是多少？',
];

const scrollToBottom = async () => {
  await nextTick();
  if (messagesRef.value) {
    messagesRef.value.scrollTop = messagesRef.value.scrollHeight;
  }
};

const autoResize = () => {
  const el = inputRef.value;
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
};

const handleSend = async () => {
  const text = inputText.value.trim();
  if (!text || streaming.value) return;
  inputText.value = '';
  if (inputRef.value) inputRef.value.style.height = 'auto';
  await sendMessage(text, scrollToBottom);
};

const handleQuickQuestion = (q) => {
  inputText.value = q;
  handleSend();
};
</script>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  max-width: 780px;
  margin: 0 auto;
  background: #f8fafc;
  font-family: -apple-system, 'PingFang SC', sans-serif;
}

/* Header */
.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  background: #fff;
  border-bottom: 1px solid #e2e8f0;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}
.header-left { display: flex; align-items: center; gap: 12px; }
.avatar {
  width: 42px; height: 42px; border-radius: 12px;
  background: linear-gradient(135deg, #2563eb, #1d4ed8);
  color: #fff; font-size: 18px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.header-info h1 { font-size: 16px; font-weight: 600; margin: 0; color: #1e293b; }
.status { font-size: 12px; color: #94a3b8; }
.status.active { color: #22c55e; }
.clear-btn {
  padding: 6px 14px; border-radius: 8px; border: 1px solid #e2e8f0;
  background: #fff; color: #64748b; cursor: pointer; font-size: 13px;
}
.clear-btn:hover { background: #f1f5f9; }

/* Messages */
.messages-wrap {
  flex: 1; overflow-y: auto; padding: 20px 16px;
  display: flex; flex-direction: column; gap: 16px;
}
.welcome {
  text-align: center; padding: 40px 20px; color: #64748b;
}
.welcome-icon { font-size: 40px; margin-bottom: 12px; }
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
.welcome p { font-size: 16px; margin: 4px 0; color: #475569; }
.welcome .sub { font-size: 13px; color: #94a3b8; }
.quick-questions {
  display: flex; flex-wrap: wrap; gap: 8px;
  justify-content: center; margin-top: 20px;
}
.quick-questions button {
  padding: 8px 14px; border-radius: 20px;
  border: 1px solid #bfdbfe; background: #eff6ff;
  color: #2563eb; font-size: 13px; cursor: pointer;
  transition: all .2s;
}
.quick-questions button:hover { background: #dbeafe; }

/* Message rows */
.message-row { display: flex; }
.message-row.user { justify-content: flex-end; }
.message-row.assistant { justify-content: flex-start; }
.bubble-wrap { display: flex; align-items: flex-end; gap: 8px; max-width: 75%; }
.message-row.user .bubble-wrap { flex-direction: row-reverse; }

.avatar-sm {
  width: 32px; height: 32px; border-radius: 10px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700;
}
.message-row.user .avatar-sm { background: #2563eb; color: #fff; }
.message-row.assistant .avatar-sm { background: #f1f5f9; color: #475569; }

.bubble {
  padding: 12px 16px; border-radius: 16px;
  line-height: 1.7; font-size: 14px;
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
.bubble p { margin: 0; white-space: pre-wrap; }
.bubble.streaming { background: #fff; }

/* 回答里的引导按钮 */
.guide-links {
  display: flex; flex-wrap: wrap; gap: 6px;
  margin-top: 10px; padding-top: 10px;
  border-top: 1px dashed #e2e8f0;
}
.guide-link {
  display: inline-flex; align-items: center;
  padding: 5px 12px; border-radius: 999px;
  background: #eff6ff; border: 1px solid #bfdbfe;
  color: #2563eb; font-size: 12px; text-decoration: none;
  transition: all .15s;
}
.guide-link:hover { background: #2563eb; border-color: #2563eb; color: #fff; }

/* 欢迎页的能力导航 */
.guide-cards {
  display: grid; gap: 10px; margin: 20px auto 0;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  max-width: 620px; text-align: left;
}
.guide-card {
  display: flex; flex-direction: column; gap: 3px;
  padding: 12px 14px; border-radius: 12px;
  background: #fff; border: 1px solid #e2e8f0;
  text-decoration: none; transition: all .15s;
}
.guide-card:hover {
  border-color: #2563eb; box-shadow: 0 4px 14px rgba(37, 99, 235, .12);
  transform: translateY(-1px);
}
.guide-card-title { font-size: 14px; font-weight: 600; color: #1e293b; }
.guide-card-desc  { font-size: 12px; color: #94a3b8; line-height: 1.5; }

.cursor {
  display: inline-block;
  animation: blink 1s step-end infinite;
  color: #2563eb; font-weight: 700;
}
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

.error-tip {
  text-align: center; padding: 10px 16px; border-radius: 8px;
  background: #fef2f2; color: #dc2626; font-size: 13px;
}

/* Input area */
.input-area {
  padding: 14px 16px;
  background: #fff;
  border-top: 1px solid #e2e8f0;
}
.input-wrap { display: flex; gap: 10px; align-items: flex-end; }
textarea {
  flex: 1; resize: none; border: 1px solid #e2e8f0;
  border-radius: 12px; padding: 10px 14px;
  font-size: 14px; font-family: inherit; line-height: 1.6;
  outline: none; transition: border-color .2s;
  background: #f8fafc; min-height: 42px; max-height: 120px;
  overflow-y: auto;
}
textarea:focus { border-color: #2563eb; background: #fff; }
textarea:disabled { opacity: 0.6; cursor: not-allowed; }

.send-btn {
  width: 70px; height: 42px; border-radius: 12px; flex-shrink: 0;
  border: none; background: #2563eb; color: #fff;
  font-size: 14px; font-weight: 600; cursor: pointer;
  transition: all .2s; display: flex; align-items: center; justify-content: center;
}
.send-btn:hover:not(:disabled) { background: #1d4ed8; }
.send-btn:disabled { background: #bfdbfe; cursor: not-allowed; }

/* Loading dots */
.dot-loading { display: flex; gap: 4px; }
.dot-loading i {
  width: 5px; height: 5px; border-radius: 50%;
  background: #fff; animation: dot 1.2s ease-in-out infinite;
}
.dot-loading i:nth-child(2) { animation-delay: .2s; }
.dot-loading i:nth-child(3) { animation-delay: .4s; }
@keyframes dot { 0%, 80%, 100% { opacity: .2; transform: scale(.8); } 40% { opacity: 1; transform: scale(1); } }

/* ════════════════════════════════════════════════════════════
   移动端适配（≤768px）：只写在这个媒体查询里，桌面端不受影响
   ════════════════════════════════════════════════════════════ */
@media (max-width: 768px) {
  /* 关键：桌面端靠 max-width + margin:auto 居中，窄屏下会被内容撑宽再被裁掉 */
  .chat-page { width: 100%; max-width: 100%; margin: 0; }
  .chat-header { padding: 10px 12px; }
  .avatar { width: 36px; height: 36px; font-size: 16px; border-radius: 10px; }
  .header-info h1 { font-size: 15px; }
  .status { font-size: 11px; }
  .clear-btn { padding: 5px 10px; font-size: 12px; }
  .messages-wrap { padding: 12px 10px; gap: 12px; }
  .bubble-wrap { max-width: 88%; }
  .bubble { padding: 10px 13px; font-size: 14px; }
  .welcome { padding: 24px 12px; }
  .guide-cards { grid-template-columns: 1fr; }
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
