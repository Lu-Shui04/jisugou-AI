<!-- client/src/views/RagView.vue -->
<template>
  <div class="rag-page">
    <header class="chat-header">
      <div class="header-left">
        <div class="avatar">购</div>
        <div>
          <h1>极速购知识库问答</h1>
          <span class="subtitle">基于商品手册和售后政策 · 回答带 [编号] 引用</span>
        </div>
      </div>
      <button @click="clearMessages">清空</button>
    </header>

    <main class="messages-wrap" ref="messagesRef">
      <div v-if="messages.length === 0" class="welcome">
        <p>您好，我可以回答关于商品规格、价格、售后政策等问题。</p>
        <p class="welcome-tip">回答里的 [1][2] 就是下方的参考来源，点编号可以对照知识库原文。</p>
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
          <div v-if="msg.loading" class="bubble loading-bubble">
            <span class="dot" /><span class="dot" /><span class="dot" />
          </div>

          <!-- 回答：把 [1][2] 渲染成可点击的引用标记 -->
          <div v-else class="bubble">
            <!-- Markdown 渲染 + [n] 引用标记（组件内按来源编号渲染成可点按钮） -->
            <MarkdownText
              v-if="msg.role === 'assistant'"
              :content="msg.content"
              :cites="srcList(msg).map((s) => s.index)"
              @cite="(index) => openSource(i, index)"
            />
            <template v-else>{{ msg.content }}</template>
          </div>

          <!-- 参考来源：默认只占一行，点某条才展开片段与原文入口 -->
          <div v-if="srcList(msg).length" class="sources-block">
            <div class="sources-head">
              <span class="sources-label">参考来源（{{ srcList(msg).length }}）</span>
              <span class="sources-hint">点击查看片段与原文</span>
            </div>

            <div class="source-chips">
              <button
                v-for="src in srcList(msg)"
                :key="src.index"
                class="source-chip"
                :class="{ active: activeKey === cardKey(i, src.index) }"
                :title="citeTitle(srcList(msg), src.index)"
                @click="toggleSource(i, src.index)"
              >
                <span class="chip-index">{{ src.index }}</span>
                <span class="chip-text">{{ src.label }}</span>
              </button>
            </div>

            <div
              v-for="src in srcList(msg)"
              v-show="activeKey === cardKey(i, src.index)"
              :key="'detail-' + src.index"
              class="source-detail"
              :ref="(el) => setCardRef(i, src.index, el)"
            >
              <div class="source-head">
                <span class="source-index">{{ src.index }}</span>
                <span class="source-file">{{ src.file }}</span>
                <span class="source-section">{{ src.section }}</span>
                <span v-if="src.score !== undefined && src.score !== null" class="source-score">
                  相似度 {{ src.score }}
                </span>
              </div>

              <pre class="source-full">{{ src.content }}</pre>

              <div class="source-actions">
                <button @click="openDoc(src)">查看原文</button>
                <button class="plain" @click="closeSource">收起</button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="error" class="error-tip">{{ error }}</div>
    </main>

    <footer class="input-area">
      <textarea
        v-model="inputText"
        placeholder="输入问题，Enter 发送"
        :disabled="loading"
        @keydown.enter.exact.prevent="handleSend"
        rows="1"
      />
      <button
        class="send-btn"
        :disabled="loading || !inputText.trim()"
        @click="handleSend"
      >
        {{ loading ? '查询中...' : '发送' }}
      </button>
    </footer>

    <!-- 原文浮层：显示知识库真实文件，并高亮命中的章节 -->
    <div v-if="doc.open" class="doc-mask" @click.self="closeDoc">
      <div class="doc-panel">
        <header class="doc-head">
          <div>
            <div class="doc-title">{{ doc.file }}</div>
            <div class="doc-sub">
              {{ doc.path }}<span v-if="doc.section"> · 命中章节：{{ doc.section }}</span>
              <span v-if="doc.score !== null && doc.score !== undefined"> · 相似度 {{ doc.score }}</span>
            </div>
          </div>
          <button class="doc-close" @click="closeDoc">关闭</button>
        </header>

        <div v-if="doc.loading" class="doc-state">正在读取知识库原文...</div>
        <div v-else-if="doc.error" class="doc-state error">{{ doc.error }}</div>
        <pre v-else class="doc-body" ref="docBodyRef" v-html="docHtml"></pre>

        <footer class="doc-foot">
          高亮部分就是本次回答引用的真实片段，可直接与上方回答核对。
        </footer>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick } from 'vue';
import { useRag } from '../composables/useRag.js';
import MarkdownText from '../components/MarkdownText.vue';
// 必须用统一的 API_BASE：这里原来写死了 http://localhost:3000/api（本地开发留下的），
// 线上点"查看原文"就变成请求用户自己电脑的 3000 端口 —— 连不上，报 Failed to fetch
import { API_BASE } from '../api.js';

const { messages, loading, error, ask, clearMessages } = useRag();

const inputText   = ref('');
const messagesRef = ref(null);
const docBodyRef  = ref(null);

// 当前展开的来源（同一屏只展开一条，避免占地方）、原文浮层
const activeKey = ref('');
const doc = ref({
  open: false, loading: false, error: '',
  file: '', path: '', section: '', score: null,
  content: '', highlight: '',
});

const quickQuestions = [
  '蓝牙耳机 X1 Pro 的续航怎么样？',
  '商品可以退货吗？',
  '机械键盘保修多久？',
  '退款需要多少天？',
];

// ── 来源归一化：兼容旧记录（只有 source / content）───────────────
const srcList = (msg) => (msg.sources || []).map((item, index) => {
  const [file, section] = String(item.source || '').split('#');
  return {
    index: item.index || index + 1,
    file: item.file || file || '知识库',
    section: item.section || section || '',
    score: item.score,
    preview: item.preview || String(item.content || '').slice(0, 120),
    content: item.content || '',
    // 紧凑列表里只显示章节名（没有章节就退回文件名）
    label: item.section || section || item.file || file || '知识库',
  };
});

// 回答里的 [1][2] 现在由 MarkdownText 组件渲染（cites 传入真实来源编号），
// 这里只保留来源标签的悬停提示
const citeTitle = (sources, index) => {
  const target = sources.find((item) => item.index === index);
  if (!target) return '';
  return target.file + (target.section ? ' · ' + target.section : '') +
    (target.score !== undefined ? ' · 相似度 ' + target.score : '');
};

// ── 片段展开 / 高亮联动 ─────────────────────────────────────────
const cardKey = (msgIndex, srcIndex) => msgIndex + '-' + srcIndex;

const cardRefs = new Map();
const setCardRef = (msgIndex, srcIndex, el) => {
  const key = cardKey(msgIndex, srcIndex);
  if (el) cardRefs.set(key, el);
  else cardRefs.delete(key);
};

const closeSource = () => { activeKey.value = ''; };

// 点来源标签：展开这一条的片段详情，再点一次收起
const toggleSource = (msgIndex, srcIndex) => {
  const key = cardKey(msgIndex, srcIndex);
  activeKey.value = activeKey.value === key ? '' : key;
};

// 点回答里的 [1]：展开对应来源并滚动过去
const openSource = async (msgIndex, srcIndex) => {
  const key = cardKey(msgIndex, srcIndex);
  activeKey.value = key;
  await nextTick();
  const card = cardRefs.get(key);
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
};

// ── 查看原文 ────────────────────────────────────────────────────
const escapeHtml = (value) => String(value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const openDoc = async (src) => {
  doc.value = {
    open: true, loading: true, error: '',
    file: src.file, path: '', section: src.section || '', score: src.score,
    content: '', highlight: src.content || '',
  };

  try {
    const query = 'name=' + encodeURIComponent(src.file) +
      '&section=' + encodeURIComponent(src.section || '');
    const response = await fetch(API_BASE + '/rag/source?' + query);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
    doc.value = { ...doc.value, loading: false, path: data.path, content: data.content };
  } catch (err) {
    // 带上实际请求地址：网络类失败（Failed to fetch）不落在服务端日志里，只能靠这行定位
    doc.value = {
      ...doc.value, loading: false,
      error: '原文加载失败：' + err.message + '（请求 ' + API_BASE + '/rag/source）',
    };
  }

  await nextTick();
  const mark = docBodyRef.value && docBodyRef.value.querySelector('.doc-mark');
  if (mark) mark.scrollIntoView({ block: 'center' });
};

const closeDoc = () => { doc.value = { ...doc.value, open: false }; };

// 原文渲染：优先高亮命中片段，找不到就高亮整节
const docHtml = computed(() => {
  const content = doc.value.content || '';
  const needle = doc.value.highlight || '';
  let start = needle ? content.indexOf(needle) : -1;
  let end = start >= 0 ? start + needle.length : -1;

  if (start < 0 && doc.value.section) {
    const head = '## ' + doc.value.section;
    const at = content.indexOf(head);
    if (at >= 0) {
      const next = content.indexOf('\n## ', at + head.length);
      start = at;
      end = next > 0 ? next : content.length;
    }
  }

  if (start < 0) return escapeHtml(content);
  return escapeHtml(content.slice(0, start)) +
    '<mark class="doc-mark">' + escapeHtml(content.slice(start, end)) + '</mark>' +
    escapeHtml(content.slice(end));
});

// ── 发送 ────────────────────────────────────────────────────────
const scrollToBottom = async () => {
  await nextTick();
  if (messagesRef.value)
    messagesRef.value.scrollTop = messagesRef.value.scrollHeight;
};

const handleSend = async () => {
  const text = inputText.value.trim();
  if (!text || loading.value) return;
  inputText.value = '';
  await ask(text, scrollToBottom);
};

const handleQuick = (q) => {
  inputText.value = q;
  handleSend();
};
</script>

<style scoped>
.rag-page {
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
  background: #0f766e; color: #fff;
  font-size: 18px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.header-left h1     { font-size: 16px; font-weight: 600; margin: 0; color: #1e293b; }
.header-left .subtitle { font-size: 12px; color: #94a3b8; }
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
.welcome p { font-size: 15px; margin: 0 0 10px; }
.welcome-tip { font-size: 12px; color: #94a3b8; margin-bottom: 16px; }
.quick-btns { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
.quick-btns button {
  padding: 7px 14px; border-radius: 20px;
  border: 1px solid #99f6e4; background: #f0fdfa;
  color: #0f766e; font-size: 13px; cursor: pointer;
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
.message-row.assistant .avatar-sm { background: #f0fdfa; color: #0f766e; }
.message-content {
  display: flex; flex-direction: column; gap: 6px; max-width: 75%;
}
.message-row.user .message-content { align-items: flex-end; }
.bubble {
  padding: 12px 16px; border-radius: 16px;
  font-size: 14px; line-height: 1.8; white-space: pre-wrap;
}
.message-row.user .bubble {
  background: #2563eb; color: #fff; border-bottom-right-radius: 4px;
}
.message-row.assistant .bubble {
  background: #fff; color: #1e293b;
  border-bottom-left-radius: 4px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}

/* 回答里的引用标记 */
.cite-chip {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 18px; height: 18px; padding: 0 5px; margin: 0 2px;
  vertical-align: 1px;
  border-radius: 6px; border: 1px solid #99f6e4; background: #f0fdfa;
  color: #0f766e; font-size: 11px; font-weight: 700;
  font-family: inherit; cursor: pointer; transition: all .15s;
}
.cite-chip:hover  { background: #0f766e; border-color: #0f766e; color: #fff; }
.cite-chip.active { background: #0f766e; border-color: #0f766e; color: #fff; }

.loading-bubble { display: flex; gap: 5px; align-items: center; min-width: 60px; }
.dot {
  width: 7px; height: 7px; border-radius: 50%; background: #94a3b8;
  animation: dot-bounce 1.2s infinite;
}
.dot:nth-child(2) { animation-delay: .2s; }
.dot:nth-child(3) { animation-delay: .4s; }
@keyframes dot-bounce {
  0%, 80%, 100% { transform: translateY(0); opacity: .4; }
  40%           { transform: translateY(-5px); opacity: 1; }
}

/* 参考来源：默认只有一行标签，点开才占地方 */
.sources-block { display: flex; flex-direction: column; gap: 6px; }
.sources-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.sources-label { font-size: 11px; color: #64748b; font-weight: 600; }
.sources-hint  { font-size: 11px; color: #cbd5e1; }

.source-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.source-chip {
  display: inline-flex; align-items: center; gap: 5px;
  max-width: 100%; padding: 3px 10px 3px 6px;
  border-radius: 999px; border: 1px solid #99f6e4; background: #f0fdfa;
  color: #0f766e; font-size: 11px; font-family: inherit;
  cursor: pointer; transition: all .15s;
}
.source-chip:hover  { border-color: #0f766e; background: #ccfbf1; }
.source-chip.active { background: #0f766e; border-color: #0f766e; color: #fff; }
.chip-index {
  width: 15px; height: 15px; border-radius: 50%; flex-shrink: 0;
  background: rgba(15, 118, 110, .12); font-weight: 700; font-size: 10px;
  display: inline-flex; align-items: center; justify-content: center;
}
.source-chip.active .chip-index { background: rgba(255, 255, 255, .22); }
.chip-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 180px; }

.source-detail {
  margin-top: 2px; padding: 10px 12px;
  background: #fff; border: 1px solid #e2e8f0; border-radius: 10px;
}
.source-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.source-index {
  width: 18px; height: 18px; border-radius: 5px; flex-shrink: 0;
  background: #f0fdfa; border: 1px solid #99f6e4; color: #0f766e;
  font-size: 11px; font-weight: 700;
  display: inline-flex; align-items: center; justify-content: center;
}
.source-file    { font-size: 12px; color: #0f766e; font-weight: 600; }
.source-section { font-size: 12px; color: #475569; }
.source-score   { font-size: 11px; color: #94a3b8; margin-left: auto; }
.source-actions { display: flex; gap: 8px; margin-top: 8px; }
.source-actions button {
  padding: 3px 10px; border-radius: 6px; font-size: 11px; cursor: pointer;
  border: 1px solid #99f6e4; background: #f0fdfa; color: #0f766e;
}
.source-actions button.plain { border-color: #e2e8f0; background: #fff; color: #64748b; }
.source-actions button:hover { border-color: #0f766e; color: #0f766e; }
.source-full {
  margin: 8px 0 0; padding: 10px; border-radius: 8px;
  background: #f8fafc; border: 1px dashed #e2e8f0;
  font-size: 12px; line-height: 1.7; color: #334155;
  white-space: pre-wrap; word-break: break-word;
  max-height: 200px; overflow-y: auto; font-family: inherit;
}

/* 原文浮层 */
.doc-mask {
  position: fixed; inset: 0; z-index: 200;
  background: rgba(15, 23, 42, .45);
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.doc-panel {
  width: min(860px, 100%); max-height: 86vh;
  background: #fff; border-radius: 14px; overflow: hidden;
  display: flex; flex-direction: column;
  box-shadow: 0 24px 60px rgba(15, 23, 42, .35);
}
.doc-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 12px; padding: 14px 18px; border-bottom: 1px solid #e2e8f0; background: #f8fafc;
}
.doc-title { font-size: 14px; font-weight: 600; color: #0f172a; }
.doc-sub   { font-size: 11px; color: #64748b; margin-top: 3px; word-break: break-all; }
.doc-close {
  border: 1px solid #e2e8f0; background: #fff; color: #64748b;
  border-radius: 8px; font-size: 12px; padding: 5px 12px; cursor: pointer; flex-shrink: 0;
}
.doc-close:hover { border-color: #0f766e; color: #0f766e; }
.doc-state { padding: 30px; text-align: center; font-size: 13px; color: #64748b; }
.doc-state.error { color: #dc2626; }
.doc-body {
  flex: 1; overflow-y: auto; margin: 0; padding: 16px 20px;
  font-size: 13px; line-height: 1.8; color: #1e293b;
  white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.doc-body :deep(.doc-mark) {
  background: #fef08a; color: #713f12; border-radius: 3px;
  padding: 1px 0; box-decoration-break: clone;
}
.doc-foot {
  padding: 10px 18px; border-top: 1px solid #e2e8f0; background: #f8fafc;
  font-size: 11px; color: #94a3b8;
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
textarea:focus    { border-color: #0f766e; background: #fff; }
textarea:disabled { opacity: 0.6; }
.send-btn {
  width: 80px; height: 42px; border-radius: 12px;
  border: none; background: #0f766e; color: #fff;
  font-size: 14px; font-weight: 600; cursor: pointer;
}
.send-btn:hover:not(:disabled) { background: #0d6b62; }
.send-btn:disabled { background: #99f6e4; color: #0f766e; cursor: not-allowed; }

/* ── 移动端适配（≤768px）：桌面端不受影响 ── */
@media (max-width: 768px) {
  .rag-page { width: 100%; max-width: 100%; margin: 0; }
  .chat-header { padding: 10px 12px; }
  .avatar { width: 36px; height: 36px; font-size: 16px; border-radius: 10px; }
  .header-left h1 { font-size: 15px; }
  .header-left .subtitle { font-size: 11px; }
  .messages-wrap { padding: 12px 10px; gap: 12px; }
  .message-content { max-width: 90%; }
  .bubble { padding: 10px 13px; font-size: 14px; }
  .welcome { padding: 24px 12px; }
  /* 来源卡片在窄屏下占满一行 */
  .source-card, .source-detail { width: 100%; }
  .chip-text { max-width: 120px; }
  /* 原文浮层：手机上留边距、别贴边 */
  .doc-mask { padding: 10px; }
  .doc-panel { max-height: 92vh; }
  .doc-body { padding: 12px 14px; font-size: 12px; }
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
