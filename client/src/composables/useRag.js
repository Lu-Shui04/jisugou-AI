// client/src/composables/useRag.js
// 状态放在模块作用域 + localStorage：切页面 / 刷新都不丢问答记录
import { ref, nextTick, watch } from 'vue';
import { useUser, ensureIdentity, onIdentityChange } from './useUser.js';
import { apiStream } from '../api.js';

// 身份：服务端登录令牌（知识库是公共数据，但身份同样由服务端认定）
const { identity } = useUser();

const MESSAGES_KEY = 'jisu:messages:rag';

function loadMessages() {
  try {
    const list = JSON.parse(localStorage.getItem(MESSAGES_KEY) || '[]');
    // 恢复时清掉"加载中"状态，避免刷新后一直转圈
    return list.map((m) => ({ ...m, loading: false }));
  } catch {
    return [];
  }
}

// ── 模块级状态 ────────────────────────────────────────────────
const messages = ref(loadMessages());
const loading = ref(false);
const error = ref('');

watch(messages, (val) => {
  try {
    localStorage.setItem(MESSAGES_KEY, JSON.stringify(val.slice(-50)));
  } catch {}
}, { deep: true });

// 换身份：知识库问答记录也按用户清掉（后台按用户看聊天记录，别串到一起）
function resetForNewIdentity() {
  messages.value = [];
  error.value = '';
  try {
    localStorage.removeItem(MESSAGES_KEY);
  } catch {}
}
onIdentityChange(resetForNewIdentity);

export function useRag() {
  const ask = async (question, scrollCallback) => {
    if (!question.trim() || loading.value) return;
    await ensureIdentity(); // 令牌就绪再发

    error.value = '';
    messages.value.push({ role: 'user', content: question });
    scrollCallback?.();

    loading.value = true;

    const assistantIndex = messages.value.length;
    messages.value.push({ role: 'assistant', content: '', sources: [], loading: true });

    try {
      // apiStream：带上 X-Gate-Token，并把 401（滑块 Token 失效）转成"重新验证"
      const response = await apiStream('/rag/query', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ question, ...identity() }),
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

            if (parsed.type === 'sources') {
              messages.value[assistantIndex] = {
                ...messages.value[assistantIndex],
                sources: parsed.sources,
              };
            }

            // 逐 token 流式分片
            if (parsed.type === 'content') {
              const current = messages.value[assistantIndex];
              messages.value[assistantIndex] = {
                role:    'assistant',
                content: (current.content || '') + parsed.content,
                sources: current.sources || [],
                loading: false,
              };
              await nextTick();
              scrollCallback?.();
            }

            // 流结束的完整回答（以它为准，避免分片丢失导致内容不全）
            if (parsed.type === 'answer') {
              messages.value[assistantIndex] = {
                role:    'assistant',
                content: parsed.content,
                sources: messages.value[assistantIndex].sources,
                loading: false,
              };
              scrollCallback?.();
            }

            if (parsed.type === 'error') {
              messages.value[assistantIndex] = {
                role: 'assistant', content: parsed.content,
                sources: [], loading: false,
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
    error.value    = '';
    localStorage.removeItem(MESSAGES_KEY);
  };

  return { messages, loading, error, ask, clearMessages };
}
