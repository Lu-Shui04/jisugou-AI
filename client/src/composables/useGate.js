// client/src/composables/useGate.js
// 开屏滑动验证的本地状态：门禁 Token、入口免验证窗口、失效回调。
//
// 门禁和后端是一对：Token 由服务端签发（POST /api/gate/verify），
// 之后每个请求带 X-Gate-Token；没带 / 过期时后端 401，前端弹回开屏滑块。
//
// 这里有两个**不同**的时间概念，别混在一起：
//   1. Token 有效期（后端签的 12 小时）—— 决定"正在用的会话会不会中途被踢"
//   2. 入口免验证窗口（下面的 GATE_ENTRY_TTL_MS）—— 决定"关掉再打开要不要重拖"
// 入口窗口只在新页面加载时检查一次，所以正在提问的人不会用着用着被弹回滑块。
const TOKEN_KEY = 'jisu:gate-token';
const VERIFIED_AT_KEY = 'jisu:gate-verified-at';

/**
 * 入口免验证窗口：距上次拖动超过这个时间，重新进入才需要再拖一次。
 *
 * 为什么是 12 小时（线上真实反馈："聊着聊着突然就被踢回滑动窗口"）：
 * 原来这里是 60 秒，而服务端签的 Token 有效期是 12 小时 —— 也就是说**只要刷新一次
 * 页面**（点"已更新到新版本"、误按 F5、开新标签页），哪怕手里的 Token 还有 11 小时
 * 有效期，也会被重新弹一次滑块，正在用的人只觉得自己被莫名踢出去了。
 * 这里改成与服务端 Token 同寿命：**Token 没过期就不打断**，过期了服务端 401、
 * 前端自然弹回滑块。
 */
export const GATE_ENTRY_TTL_MS = 12 * 60 * 60 * 1000;

export function getGateToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || '';
  } catch {
    return '';
  }
}

export function setGateToken(token) {
  try {
    localStorage.setItem(TOKEN_KEY, token || '');
    localStorage.setItem(VERIFIED_AT_KEY, String(Date.now()));
  } catch {}
}

export function clearGateToken() {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(VERIFIED_AT_KEY);
  } catch {}
}

/**
 * Token 本身是否已过期。
 * Token 格式是 "过期时间戳.签名"（见 server-py/app/security/gate.py），
 * 直接读第一段就能判断，不用发请求。缺时间戳的一律当过期。
 */
export function gateTokenExpired() {
  const token = getGateToken();
  if (!token || !token.includes('.')) return true;
  const exp = Number(token.split('.')[0]);
  if (!exp || Number.isNaN(exp)) return true;
  return exp * 1000 <= Date.now();
}

/**
 * 这次进入是否需要重新拖滑块。
 * 判据是"Token 已经过期"或"距上次拖动超过入口窗口"——
 * 手里 Token 还有效时**绝不打断**（会话中途被弹回验证页是线上真实投诉过的体验问题）。
 */
export function gateEntryExpired() {
  if (!getGateToken()) return true;
  if (gateTokenExpired()) return true;
  try {
    const at = Number(localStorage.getItem(VERIFIED_AT_KEY) || 0);
    if (!at) return false; // 旧版本留下的 Token 没有时间戳：只要没过期就继续用
    return Date.now() - at > GATE_ENTRY_TTL_MS;
  } catch {
    return true;
  }
}

// Token 失效（后端 401）时通知 App 重新弹滑块
let requiredHandler = null;

export function onGateRequired(handler) {
  requiredHandler = handler;
}

export function notifyGateRequired() {
  try {
    requiredHandler?.();
  } catch {}
}
