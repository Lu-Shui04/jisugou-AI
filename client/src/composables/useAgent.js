// client/src/composables/useAgent.js
// 状态放在模块作用域 + localStorage：切页面 / 刷新都不丢聊天记录
import { ref, nextTick, watch } from 'vue';
import { useUser, ensureIdentity, onIdentityChange, recoverIdentity } from './useUser.js';
import { API_BASE, apiStream, authHeaders } from '../api.js';

// 身份：服务端登录令牌（Authorization 头）。后端用它认定"你是谁"，
// 请求体里的 user_id 只用于后台展示 —— 订单归属校验走的是令牌里的用户 ID。
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

// 换身份：上一个用户的查询结果（订单号、金额）不能留在新用户的页面上
function resetForNewIdentity() {
  messages.value = [];
  steps.value = [];
  error.value = '';
  sessionId.value = '';
  try {
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(MESSAGES_KEY);
  } catch {}
}
onIdentityChange(resetForNewIdentity);

export function useAgent() {
  const sendMessage = async (userInput, scrollCallback) => {
    if (!userInput.trim() || loading.value) return;
    await ensureIdentity(); // 令牌就绪再发（订单页没有身份后端直接拒）

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

      // apiStream：带上 X-Gate-Token，并把 401（滑块 Token 失效）转成"重新验证"
      const response = await apiStream('/agent/stream', {
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
              // 服务端说"没认到你"（令牌失效）：重新登录一次，用户再点一次就能过
              if (parsed.code === 'UNAUTHENTICATED') recoverIdentity();
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
      fetch(`${API_BASE}/observability/session/${sessionId.value}`, {
        method: 'DELETE',
        headers: authHeaders(),
      }).catch(() => {});
    }
    sessionId.value = '';
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(MESSAGES_KEY);
  };

  return { sessionId, messages, loading, steps, error, sendMessage, clearMessages };
}
