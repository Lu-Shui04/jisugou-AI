/**
 * 第一章：Vue3 Composable
 * useChat — 封装对话逻辑，包含流式输出、历史管理
 *
 * 状态放在模块作用域 + localStorage：
 * - 切换页面（组件卸载）聊天记录不丢
 * - 刷新浏览器也能恢复
 *
 * 基础对话只会聊天和指路：模型在回答末尾输出 [[go:agent]] / [[go:rag]] / [[go:graph]]，
 * 这里把标记解析成跳转按钮，正文里不留标记。
 */
import { ref, nextTick, watch } from 'vue';
import { useUser, ensureIdentity, onIdentityChange } from './useUser.js';
import { API_BASE, apiStream, authHeaders, createSseParser } from '../api.js';

// 身份：服务端登录令牌（随每个请求的 Authorization 头带给后端，
// 后端据此认定"你是谁"；请求体里的 user_id 只用于后台展示）
const { identity } = useUser();

// 会话 ID：后端用它做 Redis 会话缓存（key = session:{session_id}，TTL 30 分钟）
const SESSION_KEY = 'jisu:session:chat';
const MESSAGES_KEY = 'jisu:messages:chat';

// 引导标记 → 页面按钮
export const GUIDE_LINKS = {
  agent: { route: '/agent', label: '去「订单查询」查订单 / 物流' },
  rag:   { route: '/rag',   label: '去「知识库」查商品 / 政策' },
  graph: { route: '/graph', label: '去「智能中枢」一次问完' },
};

const GUIDE_RE = /\[\[go:(agent|rag|graph)\]\]/g;

/** 把回答里的引导标记拆出来：正文归正文，跳转按钮归 links */
export function splitGuides(text) {
  const links = [];
  const content = String(text || '')
    .replace(GUIDE_RE, (_, key) => {
      const link = GUIDE_LINKS[key];
      if (link && !links.some((item) => item.route === link.route)) links.push(link);
      return '';
    })
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  return { content, links };
}

function loadMessages() {
  try {
    return JSON.parse(localStorage.getItem(MESSAGES_KEY) || '[]');
  } catch {
    return [];
  }
}

// ── 模块级状态：切页面不丢 ────────────────────────────────────
const sessionId = ref(localStorage.getItem(SESSION_KEY) || '');
const messages = ref(loadMessages());
const streaming = ref(false);
const streamText = ref('');
const error = ref('');

// 落到本地存储，刷新后恢复
watch(messages, (val) => {
  try {
    localStorage.setItem(MESSAGES_KEY, JSON.stringify(val.slice(-50)));
  } catch {}
}, { deep: true });

// 换身份（左上角切换用户）：上一个用户的会话与聊天记录一律清掉 ——
// 历史里可能有他的订单号，留着就会被当成上下文带进新用户的对话
function resetForNewIdentity() {
  messages.value = [];
  streamText.value = '';
  error.value = '';
  sessionId.value = '';
  try {
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(MESSAGES_KEY);
  } catch {}
}
onIdentityChange(resetForNewIdentity);

export function useChat() {
  // ─── 发送消息（流式）───────────────────────────────────────────
  const sendMessage = async (userInput, scrollCallback) => {
    if (!userInput.trim() || streaming.value) return;
    await ensureIdentity(); // 令牌就绪再发（页面刚打开就点发送也不会漏）

    error.value = '';
    messages.value.push({ role: 'user', content: userInput });
    scrollCallback?.();

    streaming.value = true;
    streamText.value = '';

    try {
      // 取最近 10 条历史，避免 Token 超限
      const history = messages.value
        .slice(-10)
        .map(({ role, content }) => ({ role, content }));

      // apiStream：带上 X-Gate-Token，并把 401（滑块 Token 失效）转成"重新验证"
      const response = await apiStream('/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: userInput,
          history,
          session_id: sessionId.value || undefined,
          ...identity(),
        }),
      });

      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      const sse = createSseParser();
      let raw = '';
      let sources = [];

      // 读取 SSE 流
      while (true) {
        const { done, value } = await reader.read();
        // done 时把缓冲里最后一行也冲出来（服务端万一没以换行收尾）
        const events = done ? sse.flush() : sse.push(decoder.decode(value, { stream: true }));

        // 跨分片缓冲解析（旧实现会把被切开的那一行整个丢掉，sources 就是这么没的）
        for (const parsed of events) {
            // 后端下发/确认的 session_id，存起来供下一轮复用
            if (parsed.type === 'session' && parsed.session_id) {
              sessionId.value = parsed.session_id;
              localStorage.setItem(SESSION_KEY, parsed.session_id);
            }
            // 安全拦截：话术已经随 content 推下来了，不再当红色错误提示
            if (parsed.type === 'blocked') {
              continue;
            }
            if (parsed.error) {
              error.value = parsed.error;
              break;
            }
            if (parsed.done) break;
            // 知识库依据（后端命中知识库时推过来的来源）
            if (parsed.type === 'sources' && Array.isArray(parsed.sources)) {
              sources = parsed.sources;
            }
            if (parsed.content) {
              raw += parsed.content;
              // 实时展示时就把引导标记摘掉，避免用户看到 [[go:agent]]
              streamText.value = splitGuides(raw).content;
              await nextTick();
              scrollCallback?.();
            }
        }
        if (done) break;
      }

      // 流结束，将完整回复（正文 + 跳转按钮）存入历史
      if (raw.trim()) {
        const { content, links } = splitGuides(raw);
        if (content || links.length) {
          messages.value.push({ role: 'assistant', content, links, sources });
        }
      }
    } catch (err) {
      error.value = `请求失败：${err.message}`;
    } finally {
      streaming.value = false;
      streamText.value = '';
      scrollCallback?.();
    }
  };

  // ─── 清空对话 ───────────────────────────────────────────────────
  const clearMessages = () => {
    messages.value = [];
    error.value = '';
    // 同时清掉服务端会话缓存
    if (sessionId.value) {
      fetch(API_BASE + '/observability/session/' + sessionId.value, {
        method: 'DELETE',
        headers: authHeaders(),
      }).catch(() => {});
    }
    sessionId.value = '';
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(MESSAGES_KEY);
  };

  return {
    sessionId,
    messages,
    streaming,
    streamText,
    error,
    sendMessage,
    clearMessages,
  };
}