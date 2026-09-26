// client/src/api.js
// 统一的 API 基地址：本地开发走 vite 代理（/api → localhost:3000），
// 服务器部署时由构建变量指定（例如 VITE_API_BASE=/jisu/api）。
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

/** 把令牌塞进请求头（Authorization: Bearer xxx） */
export function authHeaders(extra) {
  const token = getToken();
  const headers = { ...(extra || {}) };
  if (token) headers.Authorization = 'Bearer ' + token;
  return headers;
}

/** 带身份的 JSON 请求（简单接口用；SSE 流式接口自己拼 fetch） */
export async function apiFetch(path, options) {
  const opts = options || {};
  return fetch(apiUrl(path), { ...opts, headers: authHeaders(opts.headers || {}) });
}
