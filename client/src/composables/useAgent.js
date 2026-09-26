// client/src/composables/useAgent.js
// 状态放在模块作用域 + localStorage：切页面 / 刷新都不丢聊天记录
import { ref, nextTick, watch } from 'vue';
import { useUser } from './useUser.js';
import { API_BASE } from '../api.js';

// 访客身份：随请求带给后端，管理员后台按用户区分聊天记录
const { identity } = useUser();

// 会话 ID：后端用它做 Redis 会话缓存（key = session:{session_id}，TTL 30 分钟）
const SESSION_KEY = 'jisu:session:agent';
const MESSAGES_KEY = 'jisu:messages:agent';

function loadMessages() {
  try {
    const list = JSON.parse(localStorage.getItem(MESSAGES_KEY) || '[]');
    return list.map((m) => ({ ...m, thinking: false }));
  } catch {
    return [];
  }
}

// ── 模块级状态 ────────────────────────────────────────────────
const sessionId = ref(localStorage.getItem(SESSION_KEY) || '');
const messages = ref(loadMessages());
const loading  = ref(false);
const steps    = ref([]);
const error    = ref('');

watch(messages, (val) => {
  try {
    localStorage.setItem(MESSAGES_KEY, JSON.stringify(val.slice(-50)));
  } catch {}
}, { deep: true });

export function useAgent() {
  const sendMessage = async (userInput, scrollCallback) => {
    if (!userInput.trim() || loading.value) return;

    error.value = '';
    steps.value = [];
    messages.value.push({ role: 'user', content: userInput });
    scrollCallback?.();

    loading.value = true;

    const assistantIndex = messages.value.length;
    messages.value.push({ role: 'assistant', content: '', thinking: true });

    try {
      const history = messages.value
        .slice(0, -1)
        .slice(-10)
        .filter((m) => !m.thinking)
        .map(({ role, content }) => ({ role, content }));

      const response = await fetch(`${API_BASE}/agent/stream`, {
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

            if (parsed.type === 'step') {
              steps.value.push({
                tool:        parsed.tool,
                toolInput:   parsed.toolInput,
                observation: parsed.observation,
              });
              await nextTick();
              scrollCallback?.();
            }

            // 逐 token 流式分片
            if (parsed.type === 'content') {
              const current = messages.value[assistantIndex];
              messages.value[assistantIndex] = {
                role:    'assistant',
                content: (current.content || '') + parsed.content,
                thinking: false,
                steps:   [...steps.value],
              };
              await nextTick();
              scrollCallback?.();
            }

            // 进入工具调用轮：清掉模型前面那句预告，重新开始输出
            if (parsed.type === 'reset') {
              messages.value[assistantIndex] = {
                role: 'assistant', content: '', thinking: true, steps: [...steps.value],
              };
            }

            if (parsed.type === 'answer') {
              messages.value[assistantIndex] = {
                role:    'assistant',
                content: parsed.content,
                steps:   [...steps.value],
              };
              await nextTick();
              scrollCallback?.();
            }

            if (parsed.type === 'done') {
              steps.value = [];
            }

            if (parsed.type === 'error') {
              messages.value[assistantIndex] = {
                role:    'assistant',
                content: parsed.content,
              };
            }
          } catch {}
        }
      }
    } catch (err) {
      error.value = `请求失败：${err.message}`;
      messages.value.pop();
    } finally {
      loading.value = false;
    }
  };

  const clearMessages = () => {
    messages.value = [];
    steps.value    = [];
    error.value    = '';
    if (sessionId.value) {
      fetch(`${API_BASE}/observability/session/${sessionId.value}`, { method: 'DELETE' }).catch(() => {});
    }
    sessionId.value = '';
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(MESSAGES_KEY);
  };

  return { sessionId, messages, loading, steps, error, sendMessage, clearMessages };
}
