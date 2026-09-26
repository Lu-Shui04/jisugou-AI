// client/src/composables/useUser.js
// 用户身份：服务端签发的登录令牌（左上角"切换用户"= 换一个身份登录）
//
// 为什么不能像以前那样"前端随机生成一个 user_id 发给后端"：
// 那样后端只能相信客户端，谁把 user_id 改成别人就能看别人的订单（水平越权）。
// 现在身份由服务端签发令牌决定，前端只负责**出示**令牌，改不动身份。
import { ref } from 'vue';
import { apiFetch, apiUrl, authHeaders, clearToken, getToken, setToken } from '../api.js';

const USER_KEY = 'jisu:user:id';
const DEFAULT_USER_ID = 'U-100';
// 换身份时要顺手清掉的服务端会话（会话里装着上一个用户问过的订单）
const SESSION_KEYS = ['jisu:session:chat', 'jisu:session:agent', 'jisu:session:graph'];

function storedUserId() {
  try {
    return localStorage.getItem(USER_KEY) || '';
  } catch {
    return '';
  }
}

// ── 模块级状态：所有页面共用同一份身份 ─────────────────────────────
const users = ref([]);          // 可选身份 U-100 ~ U-104（后端给，带"暂无订单"这类说明）
const userId = ref(storedUserId());
const userName = ref('');
const authenticated = ref(false);
const ready = ref(false);       // 启动时自动登录完成没有
const switching = ref(false);
const error = ref('');

// 换身份 → 通知各页面清掉上一个身份留下的聊天记录 / 会话
const listeners = new Set();

export function onIdentityChange(handler) {
  listeners.add(handler);
  return () => listeners.delete(handler);
}

function notifyChange() {
  for (const handler of listeners) {
    try {
      handler();
    } catch {}
  }
}

async function fetchUsers() {
  try {
    const response = await apiFetch('/identity/users');
    if (!response.ok) return;
    const data = await response.json();
    users.value = data.users || [];
  } catch {}
}

function applyPrincipal(payload) {
  const principal = (payload && payload.principal) || {};
  if (principal.authenticated) {
    userId.value = principal.user_id;
    userName.value = principal.user_name || '';
    authenticated.value = true;
    try { localStorage.setItem(USER_KEY, principal.user_id); } catch {}
  }
}

/** 用某个身份登录，换回一枚服务端签发的令牌 */
async function login(nextUserId) {
  const response = await fetch(apiUrl('/identity/login'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ userId: nextUserId }),
  });
  if (!response.ok) {
    error.value = '登录失败（用户 ' + nextUserId + ' 不存在？）';
    return false;
  }
  const data = await response.json();
  setToken(data.token);
  userId.value = data.user.user_id;
  userName.value = data.user.name;
  authenticated.value = true;
  error.value = '';
  try { localStorage.setItem(USER_KEY, data.user.user_id); } catch {}
  return true;
}

/** 退出/换人之前：把服务端属于上一个身份的会话删掉（令牌还在，删得掉） */
async function purgeServerSessions() {
  for (const key of SESSION_KEYS) {
    let sessionId = '';
    try { sessionId = localStorage.getItem(key) || ''; } catch {}
    if (sessionId) {
      try {
        await fetch(apiUrl('/observability/session/' + sessionId), {
          method: 'DELETE',
          headers: authHeaders(),
        });
      } catch {}
    }
    try { localStorage.removeItem(key); } catch {}
  }
}

/** 启动时：有令牌先验一次，不行就用上次的身份（默认 U-100）重新登录 */
async function bootstrap() {
  await fetchUsers();
  const wanted = storedUserId();
  const fallback = users.value.some((item) => item.user_id === wanted) ? wanted : DEFAULT_USER_ID;

  if (getToken()) {
    try {
      const response = await apiFetch('/identity/me');
      if (response.ok) {
        const data = await response.json();
        if (data.authenticated) {
          applyPrincipal(data);
          ready.value = true;
          return;
        }
      }
    } catch {}
    clearToken();
  }
  await login(fallback);
  ready.value = true;
}

let booting = null;

/** 保证发送请求前身份已经就绪（页面刚打开就点发送也不会漏令牌） */
export function ensureIdentity() {
  if (!booting) booting = bootstrap();
  return booting;
}

/** 令牌被服务端拒了（密钥轮换 / 服务重启）时调一次：清掉缓存重新登录，下一次请求就能过 */
export function recoverIdentity() {
  booting = null;
  return ensureIdentity();
}

// 模块加载即开始登录（左上角切换器的选项也随之就绪）
ensureIdentity();

export function useUser() {
  /** 切换用户：换身份 → 清对方会话 → 通知各页面重置 */
  const switchUser = async (nextUserId) => {
    if (!nextUserId || switching.value) return false;
    switching.value = true;
    try {
      if (authenticated.value && nextUserId === userId.value) return true;
      await purgeServerSessions();
      const ok = await login(nextUserId);
      if (ok) notifyChange();
      return ok;
    } finally {
      switching.value = false;
    }
  };

  const logout = async () => {
    await purgeServerSessions();
    clearToken();
    authenticated.value = false;
    userName.value = '';
    notifyChange();
  };

  /** 对话请求体里带的"昵称"（只用于后台展示，后端不拿它做鉴权） */
  const identity = () => ({ user_id: userId.value, user_name: userName.value });

  return { users, userId, userName, authenticated, ready, switching, error,
           switchUser, logout, identity };
}
