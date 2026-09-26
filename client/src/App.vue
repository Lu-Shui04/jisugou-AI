<!-- client/src/App.vue -->
<template>
  <div id="app" :data-build="BUILD_TAG">
    <nav class="global-nav">
      <!-- 移动端（≤768px）左上角三条杠，点开是菜单抽屉；桌面端不显示 -->
      <button
        class="nav-burger"
        :class="{ open: menuOpen }"
        :aria-expanded="menuOpen ? 'true' : 'false'"
        aria-label="菜单"
        @click="menuOpen = !menuOpen"
      >
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
          <path v-if="!menuOpen" fill="currentColor" d="M3 6h18v2H3V6Zm0 5h18v2H3v-2Zm0 5h18v2H3v-2Z" />
          <path v-else fill="currentColor" d="M6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12 19 6.4 17.6 5 12 10.6 6.4 5Z" />
        </svg>
      </button>

      <div class="nav-left">
        <!-- 左上角管理员入口：免登录，点开直接进 -->
        <button
          class="admin-entry"
          :class="{ active: isAdminPage }"
          title="管理员后台（免登录）"
          @click="goAdmin"
        >
          <svg class="icon" viewBox="0 0 24 24" width="13" height="13" aria-hidden="true">
            <path
              fill="currentColor"
              d="M12 2a5 5 0 0 0-5 5v2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-1V7a5 5 0 0 0-5-5Zm-3 7V7a3 3 0 1 1 6 0v2H9Zm3 4a1.75 1.75 0 0 1 1 3.19V18a1 1 0 0 1-2 0v-1.81A1.75 1.75 0 0 1 12 13Z"
            />
          </svg>
          管理员入口
        </button>
        <span class="nav-brand">极速购 AI 客服</span>
      </div>

      <div class="nav-links">
        <router-link to="/">基础对话</router-link>
        <router-link to="/agent">订单查询</router-link>
        <router-link to="/rag">知识库</router-link>
        <router-link to="/graph">智能中枢</router-link>
        <span class="nav-user" :title="'访客标识：' + userId + '（点击修改昵称）'" @click="renameUser">
          {{ userName }}
        </span>
      </div>
    </nav>

    <!-- 移动端菜单抽屉：管理员入口 / 四个页面 / 昵称 -->
    <div v-if="menuOpen" class="nav-drawer" @click="menuOpen = false">
      <div class="drawer-inner" @click.stop>
        <router-link to="/" @click="menuOpen = false"><span>💬</span>基础对话</router-link>
        <router-link to="/agent" @click="menuOpen = false"><span>📦</span>订单查询</router-link>
        <router-link to="/rag" @click="menuOpen = false"><span>📚</span>知识库</router-link>
        <router-link to="/graph" @click="menuOpen = false"><span>🧭</span>智能中枢</router-link>
        <router-link to="/admin" class="drawer-admin" @click="menuOpen = false"><span>🔒</span>管理员后台</router-link>
        <div class="drawer-user" @click="menuOpen = false; renameUser()">
          <span>👤</span>{{ userName }}（点击改昵称）
        </div>
      </div>
    </div>

    <router-view />

    <!-- 页面是用旧版本资源打开的：提醒刷新一次（后端已经更新，前端还是旧 JS） -->
    <div v-if="updateReady" class="update-tip" @click="reloadPage">
      🔄 已更新到新版本，点这里刷新
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useUser } from './composables/useUser.js';

const route  = useRoute();
const router = useRouter();
const { userId, userName, rename } = useUser();

// 构建标记：只挂在 DOM 属性上，便于确认线上跑的是哪一版
const BUILD_TAG = '2026-09-25.2';

const isAdminPage = computed(() => route.path.startsWith('/admin'));

// 移动端菜单抽屉
const menuOpen = ref(false);
watch(() => route.path, () => { menuOpen.value = false; });

const goAdmin = () => {
  menuOpen.value = false;
  router.push('/admin');
};

// 昵称用于管理员后台区分「不同用户与 AI 的聊天记录」
const renameUser = () => {
  const next = window.prompt('修改昵称（管理员后台按昵称区分用户）', userName.value);
  if (next !== null) rename(next);
};

// ── 版本检测 ──────────────────────────────────────────────────────
// 单页应用不会自己换新代码：我这边重新部署后，你还开着旧页面就会"看不到改动"。
// 这里每隔一分钟（以及切回标签页时）看一眼线上 index.html 引用的 JS 名字，
// 和自己正在跑的不是同一个就提示刷新。资源几乎不占带宽（一个 400 字节的 HTML）。
const updateReady = ref(false);
let loadedAsset = '';

// 只比对文件名（index-xxxx.js），因为线上 HTML 里写的是相对路径、
// DOM 里取到的是带前缀的完整路径（/jisu/assets/…），直接比字符串会永远"不相等"
const assetName = (path) => (String(path || '').match(/index-[A-Za-z0-9_-]+\.js/) || [''])[0];

const currentScript = () => {
  const script = [...document.querySelectorAll('script[src]')]
    .map((item) => item.getAttribute('src') || '')
    .find((src) => src.includes('index-'));
  return assetName(script);
};

const checkVersion = async () => {
  try {
    const url = import.meta.env.BASE_URL + 'index.html?ts=' + Date.now();
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) return;
    const html = await response.text();
    const latest = assetName(html);
    if (!latest) return;
    const loaded = currentScript();
    if (!loadedAsset) {
      loadedAsset = loaded;
      if (loaded && latest !== loaded) updateReady.value = true;
      return;
    }
    updateReady.value = latest !== loadedAsset;
  } catch {
    // 检测失败不影响使用
  }
};

const reloadPage = () => window.location.reload();

onMounted(() => {
  checkVersion();
  setInterval(checkVersion, 60000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) checkVersion();
  });
});
</script>

<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, 'PingFang SC', sans-serif; background: #f8fafc; }

/* 新版本提示：页面还是旧资源时挂在底部中间，点一下刷新 */
.update-tip {
  position: fixed; left: 50%; bottom: 16px; transform: translateX(-50%);
  z-index: 999; cursor: pointer;
  padding: 8px 16px; border-radius: 999px;
  background: #2563eb; color: #fff; font-size: 13px; font-weight: 600;
  box-shadow: 0 6px 18px rgba(37, 99, 235, .35);
}
.update-tip:hover { background: #1d4ed8; }

.global-nav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  height: 48px;
  background: #1e293b;
  position: sticky;
  top: 0;
  z-index: 100;
}
.nav-left { display: flex; align-items: center; gap: 12px; }
.nav-brand {
  color: #fff;
  font-size: 15px;
  font-weight: 600;
}

/* 左上角管理员入口按钮 */
.admin-entry {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 10px;
  border-radius: 8px;
  border: 1px solid #475569;
  background: #0f172a;
  color: #cbd5e1;
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  transition: all .15s;
}
.admin-entry:hover  { border-color: #38bdf8; color: #fff; background: #172033; }
.admin-entry.active { border-color: #2563eb; background: #2563eb; color: #fff; }
.admin-entry .icon  { flex-shrink: 0; }

.nav-links {
  display: flex;
  align-items: center;
  gap: 4px;
}
.nav-links a {
  color: #94a3b8;
  text-decoration: none;
  font-size: 13px;
  padding: 6px 12px;
  border-radius: 6px;
  transition: all .15s;
}
.nav-links a:hover        { color: #fff; background: #334155; }
.nav-links a.router-link-active { color: #fff; background: #2563eb; }

.nav-user {
  margin-left: 8px;
  font-size: 12px;
  color: #cbd5e1;
  background: #334155;
  border-radius: 12px;
  padding: 4px 10px;
  cursor: pointer;
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.nav-user:hover { background: #475569; color: #fff; }

#app {
  display: flex;
  flex-direction: column;
  height: 100vh;
}
#app > .router-view,
#app > div:not(.global-nav):not(.nav-drawer) {
  flex: 1;
  overflow: hidden;
}

/* 三条杠与抽屉默认不出现（只有移动端媒体查询里才显示） */
.nav-burger { display: none; }
.nav-drawer { display: none; }

/* ════════════════════════════════════════════════════════════
   移动端适配（≤768px）：全部写在这个媒体查询里，桌面端一行不受影响
   ════════════════════════════════════════════════════════════ */
@media (max-width: 768px) {
  .global-nav {
    height: 52px;
    padding: 0 12px;
    gap: 8px;
    justify-content: flex-start;
  }
  .nav-burger {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    flex-shrink: 0;
    border-radius: 9px;
    border: 1px solid #475569;
    background: #0f172a;
    color: #e2e8f0;
    cursor: pointer;
  }
  .nav-burger.open { background: #2563eb; border-color: #2563eb; color: #fff; }

  /* 桌面端的入口按钮与链接行收进抽屉 */
  .admin-entry { display: none; }
  .nav-links   { display: none; }
  .nav-left    { gap: 8px; min-width: 0; }
  .nav-brand   { font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .nav-drawer {
    display: block;
    position: fixed;
    inset: 52px 0 0 0;
    background: rgba(15, 23, 42, .45);
    z-index: 99;
  }
  .drawer-inner {
    background: #1e293b;
    padding: 8px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    box-shadow: 0 12px 24px rgba(15, 23, 42, .35);
    max-height: calc(100vh - 52px);
    overflow-y: auto;
  }
  .drawer-inner a,
  .drawer-user {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 13px 14px;
    border-radius: 10px;
    color: #cbd5e1;
    text-decoration: none;
    font-size: 15px;
    background: #172033;
    cursor: pointer;
  }
  .drawer-inner a.router-link-active { background: #2563eb; color: #fff; }
  .drawer-inner a span,
  .drawer-user span { font-size: 16px; }
  .drawer-admin { border: 1px solid #334155; }
  .drawer-user  { color: #94a3b8; font-size: 13px; }
}
</style>
