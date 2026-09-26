// client/src/composables/useAdmin.js
// 管理员后台接口封装：登录拿令牌，之后所有请求带 X-Admin-Token
import { ref } from 'vue';

import { API_BASE } from '../api.js';
const TOKEN_KEY = 'jisu:admin:token';
const NAME_KEY  = 'jisu:admin:name';

const token     = ref(localStorage.getItem(TOKEN_KEY) || '');
const adminName = ref(localStorage.getItem(NAME_KEY) || '');
// 后台免登录：探测到接口可直接访问时置为 true，页面不再显示登录表单
const authOpened = ref(false);

function clearSession() {
  token.value     = '';
  adminName.value = '';
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(NAME_KEY);
}

function saveSession(nextToken, username) {
  token.value     = nextToken;
  adminName.value = username || 'admin';
  localStorage.setItem(TOKEN_KEY, token.value);
  localStorage.setItem(NAME_KEY, adminName.value);
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers['Content-Type'] = 'application/json';
  if (token.value)  headers['X-Admin-Token'] = token.value;

  const response = await fetch(API_BASE + '/admin' + path, { ...options, headers });

  if (response.status === 401) {
    clearSession();
    throw new Error('登录已过期，请重新登录');
  }
  if (!response.ok) {
    let detail = 'HTTP ' + response.status;
    try {
      const data = await response.json();
      if (data && data.detail) detail = data.detail;
    } catch {}
    throw new Error(detail);
  }
  return response.json();
}

export function useAdmin() {
  /** 探测后台是否需要登录；免登录时直接放行 */
  const ensureAccess = async () => {
    if (token.value) return true;
    try {
      const data = await request('/me');
      authOpened.value = true;
      if (data && data.username) adminName.value = data.username;
      return true;
    } catch {
      return false;
    }
  };

  const login = async (username, password) => {
    const data = await request('/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    saveSession(data.token, data.username || username);
    return data;
  };

  const logout = async () => {
    try {
      if (token.value) await request('/logout', { method: 'POST' });
    } catch {}
    clearSession();
  };

  /** 导出 CSV：带令牌请求后触发浏览器下载 */
  const downloadConversations = async (query = {}) => {
    const params = new URLSearchParams(
      Object.entries(query).filter(([, v]) => v !== '' && v !== null && v !== undefined)
    );
    const response = await fetch(API_BASE + '/admin/export/conversations.csv?' + params, {
      headers: { 'X-Admin-Token': token.value },
    });
    if (!response.ok) throw new Error('导出失败：HTTP ' + response.status);
    const blob = await response.blob();
    const url  = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'conversations-' + new Date().toISOString().slice(0, 10) + '.csv';
    link.click();
    URL.revokeObjectURL(url);
  };

  return {
    token,
    adminName,
    isLoggedIn: () => !!token.value || authOpened.value,
    ensureAccess,
    login,
    logout,
    request,
    downloadConversations,
  };
}
