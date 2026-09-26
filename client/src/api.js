// client/src/api.js
// 统一的 API 基地址：本地开发走 vite 代理（/api → localhost:3000），
// 服务器部署时由构建变量指定（例如 VITE_API_BASE=/jisu/api）。
export const API_BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '');

/** 拼接口地址，path 以 / 开头，例如 apiUrl('/chat/stream') */
export function apiUrl(path) {
  return API_BASE + (path.startsWith('/') ? path : '/' + path);
}
