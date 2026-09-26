// client/src/composables/useUser.js
// 访客身份：前端生成并持久化的用户标识，随每次对话请求带给后端，
// 管理员后台据此区分「不同用户与 AI 的聊天记录」。
// （当前项目没有账号体系，昵称只是本地标识，可点击导航栏昵称修改）
import { ref } from 'vue';

const USER_ID_KEY = 'jisu:user:id';
const USER_NAME_KEY = 'jisu:user:name';

function randomId() {
  return 'u_' + Math.random().toString(36).slice(2, 8) + Date.now().toString(36).slice(-4);
}

function defaultName(id) {
  return '访客' + id.slice(-4);
}

let id = '';
try {
  id = localStorage.getItem(USER_ID_KEY) || '';
} catch {}
if (!id) {
  id = randomId();
  try { localStorage.setItem(USER_ID_KEY, id); } catch {}
}

let name = '';
try {
  name = localStorage.getItem(USER_NAME_KEY) || '';
} catch {}
if (!name) {
  name = defaultName(id);
  try { localStorage.setItem(USER_NAME_KEY, name); } catch {}
}

// 模块级状态：所有页面共用同一个身份
const userId = ref(id);
const userName = ref(name);

export function useUser() {
  const rename = (next) => {
    const value = (next || '').trim().slice(0, 20);
    if (!value) return userName.value;
    userName.value = value;
    try { localStorage.setItem(USER_NAME_KEY, value); } catch {}
    return value;
  };

  /** 对话请求体里统一带上用户信息 */
  const identity = () => ({ user_id: userId.value, user_name: userName.value });

  return { userId, userName, rename, identity };
}
