// client/src/composables/useGraph.js
// 状态放在模块作用域 + localStorage：切页面 / 刷新都不丢聊天记录
import { ref, nextTick, watch } from 'vue';
import { useUser, ensureIdentity, onIdentityChange, recoverIdentity } from './useUser.js';
import { API_BASE, apiStream, authHeaders, createSseParser } from '../api.js';

// 身份：服务端登录令牌（Authorization 头）；智能中枢里也有订单节点，同样按真实用户校验
const { identity } = useUser();

export const NODE_LABELS = {
  intentRouter:      '意图识别',
  orderAgent:        '订单查询',
  ragNode:           '知识库检索',
  generalChat:       '通用对话',
  answerSynthesizer: '整理回答',
};

export const INTENT_LABELS = {
  order:     '订单查询',
  knowledge: '知识库问答',
  general:   '通用对话',
};

// 会话 ID：后端用它做 Redis 会话缓存（key = session:{session_id}，TTL 30 分钟）
const SESSION_KEY = 'jisu:session:graph';
const MESSAGES_KEY = 'jisu:messages:graph';

function loadMessages() {
  try {
    const list = JSON.parse(localStorage.getItem(MESSAGES_KEY) || '[]');
    return list.map((m) => ({ ...m, loading: false }));
  } catch {
    return [];
  }
}

// ── 模块级状态 ────────────────────────────────────────────────
const sessionId   = ref(localStorage.getItem(SESSION_KEY) || '');
const messages    = ref(loadMessages());
const loading     = ref(false);
const currentNode = ref('');
const error       = ref('');

watch(messages, (val) => {
  try {
    localStorage.setItem(MESSAGES_KEY, JSON.stringify(val.slice(-50)));
  } catch {}
}, { deep: true });

// 换身份：清掉上一个用户的对话与会话（里面可能有他的订单）
function resetForNewIdentity() {
  messages.value    = [];
  currentNode.value = '';
  error.value       = '';
  sessionId.value   = '';
  try {
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(MESSAGES_KEY);
  } catch {}
}
onIdentityChange(resetForNewIdentity);

export function useGraph() {
  const sendMessage = async (userInput, scrollCallback) => {
    if (!userInput.trim() || loading.value) return;
    await ensureIdentity(); // 令牌就绪再发

    error.value       = '';
    currentNode.value = '';
    messages.value.push({ role: 'user', content: userInput });
    scrollCallback?.();

    loading.value = true;

    const assistantIndex = messages.value.length;
    messages.value.push({
      role: 'assistant', content: '',
      nodes: [], intent: '', steps: [], loading: true,
    });

    try {
      const history = messages.value
        .slice(0, -1).slice(-8)
        .filter((m) => !m.loading)
        .map(({ role, content }) => ({ role, content }));

      // apiStream：带上 X-Gate-Token，并把 401（滑块 Token 失效）转成"重新验证"
      const response = await apiStream('/graph/stream', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          message: userInput,
          history,
          session_id: sessionId.value || undefined,
          ...identity(),
        }),
      });

      const reader  = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      const sse = createSseParser();

      while (true) {
        const { done, value } = await reader.read();
        // done 时把缓冲里最后一行也冲出来（服务端万一没以换行收尾）
        const events = done ? sse.flush() : sse.push(decoder.decode(value, { stream: true }));

        // 跨分片缓冲解析（旧实现会把被切开的那一行整个丢掉）
        for (const parsed of events) {
            if (parsed.type === 'session' && parsed.session_id) {
              sessionId.value = parsed.session_id;
              localStorage.setItem(SESSION_KEY, parsed.session_id);
            }

            if (parsed.type === 'node') {
              currentNode.value = NODE_LABELS[parsed.node] || parsed.node;
              const msg = messages.value[assistantIndex];

              // 一句话可能同时命中多个意图，标签用「 + 」拼起来
              const intentText = (parsed.intents && parsed.intents.length)
                ? parsed.intents.map((key) => INTENT_LABELS[key] || key).join(' + ')
                : (parsed.intent ? (INTENT_LABELS[parsed.intent] || parsed.intent) : '');

              if (!msg.nodes.includes(parsed.node)) {
                messages.value[assistantIndex] = {
                  ...msg,
                  nodes:  [...msg.nodes, parsed.node],
                  intent: intentText || msg.intent,
                };
              }
              await nextTick();
              scrollCallback?.();
            }

            if (parsed.type === 'steps') {
              messages.value[assistantIndex] = {
                ...messages.value[assistantIndex],
                steps: parsed.steps,
              };
            }

            // 知识库依据：回答了知识库内容就标出处，用户能核对
            if (parsed.type === 'sources' && Array.isArray(parsed.sources)) {
              messages.value[assistantIndex] = {
                ...messages.value[assistantIndex],
                sources: parsed.sources,
              };
            }

            // 逐 token 流式分片（只来自最终答案节点）
            if (parsed.type === 'content') {
              const current = messages.value[assistantIndex];
              messages.value[assistantIndex] = {
                ...current,
                content: (current.content || '') + parsed.content,
                loading: false,
              };
              currentNode.value = '';
              await nextTick();
              scrollCallback?.();
            }

            if (parsed.type === 'answer') {
              messages.value[assistantIndex] = {
                ...messages.value[assistantIndex],
                content: parsed.content,
                loading: false,
              };
              currentNode.value = '';
              await nextTick();
              scrollCallback?.();
            }

            if (parsed.type === 'error') {
              // 服务端说"没认到你"（令牌失效）：重新登录一次，用户再点一次就能过
              if (parsed.code === 'UNAUTHENTICATED') recoverIdentity();
              messages.value[assistantIndex] = {
                ...messages.value[assistantIndex],
                content: parsed.content,
                loading: false,
              };
              currentNode.value = '';
            }
        }
      }
    } catch (err) {
      error.value = `请求失败：${err.message}`;
      messages.value.pop();
    } finally {
      loading.value     = false;
      currentNode.value = '';
    }
  };

  const clearMessages = () => {
    messages.value    = [];
    currentNode.value = '';
    error.value       = '';
    if (sessionId.value) {
      fetch(`${API_BASE}/observability/session/${sessionId.value}`, {
        method: 'DELETE',
        headers: authHeaders(),
      }).catch(() => {});
    }
    sessionId.value = '';
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(MESSAGES_KEY);
  };

  return { sessionId, messages, loading, currentNode, error, sendMessage, clearMessages };
}
