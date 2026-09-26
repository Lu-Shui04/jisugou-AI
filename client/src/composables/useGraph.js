// client/src/composables/useGraph.js
// 状态放在模块作用域 + localStorage：切页面 / 刷新都不丢聊天记录
import { ref, nextTick, watch } from 'vue';
import { useUser } from './useUser.js';
import { API_BASE } from '../api.js';

// 访客身份：随请求带给后端，管理员后台按用户区分聊天记录
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

export function useGraph() {
  const sendMessage = async (userInput, scrollCallback) => {
    if (!userInput.trim() || loading.value) return;

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

      const response = await fetch(`${API_BASE}/graph/stream`, {
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

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const lines = decoder
          .decode(value, { stream: true })
          .split('\n')
          .filter((l) => l.startsWith('data: '));

        for (const line of lines) {
          try {
            const parsed = JSON.parse(line.slice(6));

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
              messages.value[assistantIndex] = {
                ...messages.value[assistantIndex],
                content: parsed.content,
                loading: false,
              };
              currentNode.value = '';
            }
          } catch {}
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
      fetch(`${API_BASE}/observability/session/${sessionId.value}`, { method: 'DELETE' }).catch(() => {});
    }
    sessionId.value = '';
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(MESSAGES_KEY);
  };

  return { sessionId, messages, loading, currentNode, error, sendMessage, clearMessages };
}
