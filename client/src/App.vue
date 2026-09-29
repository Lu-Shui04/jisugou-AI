<!-- client/src/App.vue -->
<template>
  <!-- 开屏滑动验证：没过滑块之前，整个应用（导航 / 四个页面 / 后台）都不渲染。
       Token 由服务端签发，之后每个请求带 X-Gate-Token；失效时自动弹回这里。 -->
  <SliderGate v-if="!verified" @passed="onGatePassed" />

  <div v-else id="app" :data-build="BUILD_TAG">
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
        <!-- 左上角身份切换：进系统第一步就是选"我是谁"。
             选完由服务端签发令牌，之后每个请求都带着它；订单数据只认这个身份，
             请求体里改 user_id 没用（后端拿令牌里的用户 ID 做校验）。 -->
        <label class="user-switch" :title="switchTitle">
          <svg class="icon" viewBox="0 0 24 24" width="13" height="13" aria-hidden="true">
            <path
              fill="currentColor"
              d="M12 12a5 5 0 1 0 0-10 5 5 0 0 0 0 10Zm0 2c-4.42 0-8 2.24-8 5v1h16v-1c0-2.76-3.58-5-8-5Z"
            />
          </svg>
          <select :value="userId" :disabled="!ready || switching" @change="onSwitchUser">
            <option v-for="item in users" :key="item.user_id" :value="item.user_id">
              {{ item.user_id }} · {{ item.name }}{{ item.note ? '（' + item.note + '）' : '' }}
            </option>
          </select>
        </label>

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
        <span class="nav-user" :title="switchTitle">{{ userName || '未登录' }}</span>
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
        <div class="drawer-user">
          <span>👤</span>
          <select :value="userId" :disabled="!ready || switching" @change="onSwitchUser">
            <option v-for="item in users" :key="item.user_id" :value="item.user_id">
              {{ item.user_id }} · {{ item.name }}{{ item.note ? '（' + item.note + '）' : '' }}
            </option>
          </select>
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
import SliderGate from './components/SliderGate.vue';
import { clearGateToken, gateEntryExpired, onGateRequired } from './composables/useGate.js';

const route  = useRoute();
const router = useRouter();
// 身份：服务端签发的登录令牌（左上角切换用户 = 换一个身份登录）
const { userId, userName, users, ready, switching, switchUser } = useUser();

// 构建标记：只挂在 DOM 属性上，便于确认线上跑的是哪一版
const BUILD_TAG = '2026-09-27.1';

// ── 开屏门禁 ──────────────────────────────────────────────────────
// 手里的门禁 Token 还有效（12 小时）就直接进，**刷新页面也不打断**；
// 只有没 Token（首次/清过缓存）或 Token 真的过期了才弹滑块。
// Token 失效时（后端 401）由 api.js 回调把 verified 置回 false，自动弹回滑块。
const verified = ref(!gateEntryExpired());
if (!verified.value) clearGateToken();

const onGatePassed = () => { verified.value = true; };
onGateRequired(() => { verified.value = false; });

const isAdminPage = computed(() => route.path.startsWith('/admin'));

// 移动端菜单抽屉
const menuOpen = ref(false);
watch(() => route.path, () => { menuOpen.value = false; });

const goAdmin = () => {
  menuOpen.value = false;
  router.push('/admin');
};

// 切换用户：换一枚服务端令牌，并清掉上一个身份留下的会话与聊天记录
const switchTitle = computed(() => (ready.value
  ? `当前身份：${userId.value} ${userName.value}（订单数据只认这个身份）`
  : '正在登录…'));

const onSwitchUser = async (event) => {
  const next = event.target.value;
  const ok = await switchUser(next);
  if (!ok) {
    event.target.value = userId.value;
    window.alert('切换用户失败，请重试');
  }
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

/* 左上角身份切换器（谁在看这份数据，由它决定） */
.user-switch {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 8px;
  border-radius: 8px;
  border: 1px solid #475569;
  background: #0f172a;
  color: #cbd5e1;
  cursor: pointer;
  transition: all .15s;
}
.user-switch:hover { border-color: #38bdf8; }
.user-switch select {
  background: transparent;
  border: 0;
  color: #e2e8f0;
  font-size: 12px;
  font-family: inherit;
  outline: none;
  cursor: pointer;
  max-width: 170px;
}
.user-switch select:disabled { color: #94a3b8; cursor: progress; }
.user-switch select option { background: #0f172a; color: #e2e8f0; }

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

  /* 桌面端的身份切换、入口按钮与链接行收进抽屉 */
  .user-switch { display: none; }
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
  .drawer-user select {
    flex: 1;
    background: transparent;
    border: 0;
    color: #e2e8f0;
    font-size: 14px;
    font-family: inherit;
    outline: none;
  }
  .drawer-user select option { background: #172033; color: #e2e8f0; }
}
</style>
