// client/src/api.js
// 统一的 API 基地址：本地开发走 vite 代理（/api → localhost:3000），
// 服务器部署时由构建变量指定（例如 VITE_API_BASE=/jisu/api）。
import { clearGateToken, getGateToken, notifyGateRequired } from './composables/useGate.js';

export const API_BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '');

/** 拼接口地址，path 以 / 开头，例如 apiUrl('/chat/stream') */
export function apiUrl(path) {
  return API_BASE + (path.startsWith('/') ? path : '/' + path);
}

// ── 身份令牌（权限系统的凭证）─────────────────────────────────────
// 登录时由服务端签发（HMAC 签名），之后每个请求都带上它。
// 后端只认令牌里的身份；请求体里的 user_id 只是"客户端声称"，不参与鉴权。
// 令牌改了/过期了，服务端验签不过 → 当匿名处理 → 查不到任何订单数据。
const TOKEN_KEY = 'jisu:identity:token';

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || '';
  } catch {
    return '';
  }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {}
}

export function clearToken() {
  setToken('');
}

/** 把两枚令牌塞进请求头：
 *  - Authorization: Bearer xxx  —— 我是谁（订单数据只认它）
 *  - X-Gate-Token: xxx          —— 已通过开屏滑块（烧钱的入口只认它）
 */
export function authHeaders(extra) {
  const token = getToken();
  const headers = { ...(extra || {}) };
  if (token) headers.Authorization = 'Bearer ' + token;
  const gateToken = getGateToken();
  if (gateToken) headers['X-Gate-Token'] = gateToken;
  return headers;
}

/** 带身份的 JSON 请求（简单接口用；SSE 流式接口用 apiStream） */
export async function apiFetch(path, options) {
  const opts = options || {};
  return fetch(apiUrl(path), { ...opts, headers: authHeaders(opts.headers || {}) });
}

// ── 开屏滑块（人机验证）───────────────────────────────────────────
// 服务端实现在 server-py/app/security/gate.py：一次性 challenge + 签名 Token。
export const gateApi = {
  /** 取一次性 challenge（5 分钟有效、只能用一次） */
  async challenge() {
    const response = await apiFetch('/gate/challenge');
    if (!response.ok) throw new Error('无法连接服务');
    return response.json();
  },
  /** 提交拖动结果（耗时 + 轨迹点数），换取 Token */
  async verify(challenge_id, duration_ms, points) {
    const response = await apiFetch('/gate/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ challenge_id, duration_ms, points }),
    });
    if (!response.ok) {
      let detail = '验证未通过，请重试';
      try {
        const body = await response.json();
        if (body && body.detail) detail = String(body.detail);
      } catch {}
      throw new Error(detail);
    }
    return response.json();
  },
};

/** 门禁失效？后端 401 = 滑块 Token 没了/过期：清掉并通知 App 弹回开屏 */
export function gateExpired(response) {
  if (response.status !== 401) return false;
  clearGateToken();
  notifyGateRequired();
  return true;
}

/** 走门禁的流式请求（四个烧额度的入口）：401 不当成"网络错误"，而是要重新过滑块 */
export async function apiStream(path, options) {
  const opts = options || {};
  const response = await fetch(apiUrl(path), {
    ...opts,
    headers: authHeaders(opts.headers || {}),
  });
  if (gateExpired(response)) throw new Error('验证已过期，请重新拖动滑块');
  return response;
}
