<!-- client/src/components/SliderGate.vue -->
<!--
  开屏人机验证（滑块）：跟 docrag 那套同款 —— 拖动滑块进入，无需账号密码。

  为什么不是密码：这个站是给人点开看的，密码会卡住第一屏，写出去等于没有。
  为什么不是纯前端滑块：F12 就能绕过，脚本直接调烧钱的接口照样刷额度。
  所以拖动结束后把「耗时 + 轨迹点数 + 一次性 challenge」交给服务端校验，
  通过才签发 Token，之后每个请求带 X-Gate-Token。

  交互细节（与服务端阈值对应，见 server-py/app/security/gate.py）：
    - 拖到 92% 以上才算过，松手回弹
    - 耗时 ≥200ms、轨迹点 ≥5 才算"人拖的"（脚本常见 <50ms、0 轨迹）
    - 失败自动换一个 challenge 让用户重试
-->
<template>
  <div class="gate">
    <div class="gate-card">
      <div class="gate-brand">
        <div class="brand-mark">购</div>
        <div>
          <h1>极速购 AI 客服</h1>
          <p>订单查询 · 知识库问答 · 智能中枢</p>
        </div>
      </div>

      <div class="gate-desc">
        这是极速购的公开演示站。为防止接口被脚本滥用，
        <strong>拖动下方滑块</strong>即可进入 —— 无需账号密码。
      </div>

      <div
        ref="trackRef"
        class="slider"
        :class="{ 'is-dragging': dragging, 'is-done': phase === 'ok', 'is-fail': phase === 'fail' }"
      >
        <div class="slider-fill" :style="{ width: pct + '%' }" />
        <span class="slider-text" :style="{ opacity: Math.max(0, 1 - pct / 130) }">
          {{ label }}
        </span>
        <div
          class="slider-handle"
          :style="{
            transform: 'translateX(' + x + 'px)',
            transition: dragging ? 'none' : 'transform .32s cubic-bezier(.2,.75,.2,1)',
          }"
          role="slider"
          aria-label="拖动以验证"
          :aria-valuenow="Math.round(pct)"
          tabindex="0"
          @pointerdown="onDown"
          @pointermove="onMove"
          @pointerup="onUp"
          @pointercancel="onUp"
        >
          <svg v-if="phase === 'checking'" class="spin" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
            <path fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"
                  d="M12 3a9 9 0 1 0 9 9" />
          </svg>
          <svg v-else-if="phase === 'ok'" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
            <path fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"
                  d="m4.5 12.5 5 5 10-11" />
          </svg>
          <svg v-else-if="phase === 'fail'" viewBox="0 0 24 24" width="17" height="17" aria-hidden="true">
            <path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"
                  d="M3.5 8.5V4h4.5M20.5 15.5V20H16M20 9a8 8 0 0 0-14.3-2.6M4 15a8 8 0 0 0 14.3 2.6" />
          </svg>
          <svg v-else viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
            <path fill="currentColor" d="M13.2 5 11.8 6.4 17.4 12l-5.6 5.6 1.4 1.4 7-7-7-7Z" />
            <path fill="currentColor" d="M5 11h8v2H5z" />
          </svg>
        </div>
      </div>

      <div v-if="err" class="gate-err">{{ err }}</div>

      <div class="gate-foot">
        <svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true">
          <path fill="currentColor"
                d="M12 2 4 5.5v6c0 5 3.4 9.3 8 10.5 4.6-1.2 8-5.5 8-10.5v-6L12 2Zm-1 13.4-3-3 1.4-1.4 1.6 1.6 4.2-4.2 1.4 1.4-5.6 5.6Z" />
        </svg>
        <span>轻量防滥用验证，不是登录；验证后 12 小时内免验证，处理中途不会被打断</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { gateApi } from '../api.js';
import { setGateToken } from '../composables/useGate.js';

const emit = defineEmits(['passed']);

// loading 取 challenge / idle 等拖 / checking 服务端校验 / ok 通过 / fail 失败
const phase = ref('loading');
const x = ref(0);
const dragging = ref(false);
const err = ref('');

const trackRef = ref(null);
const challengeId = ref('');
const startedAt = ref(0);
const points = ref(0);
const maxX = ref(0);

const HANDLE = 44;

const measure = () => {
  const width = trackRef.value?.clientWidth || 0;
  maxX.value = Math.max(0, width - HANDLE - 8);
};

const loadChallenge = async () => {
  phase.value = 'loading';
  err.value = '';
  x.value = 0;
  try {
    const data = await gateApi.challenge();
    challengeId.value = data.challenge_id;
    phase.value = 'idle';
  } catch (e) {
    err.value = e?.message || '无法连接服务';
    phase.value = 'fail';
  }
};

const finish = async () => {
  phase.value = 'checking';
  const duration = Date.now() - startedAt.value;
  try {
    const data = await gateApi.verify(challengeId.value, duration, points.value);
    setGateToken(data.token);
    phase.value = 'ok';
    setTimeout(() => emit('passed'), 620);
  } catch (e) {
    err.value = e?.message || '验证未通过';
    phase.value = 'fail';
    setTimeout(() => {
      x.value = 0;
      loadChallenge();
    }, 1100);
  }
};

const onDown = (event) => {
  if (phase.value !== 'idle' && phase.value !== 'fail') return;
  // 指针捕获：拖出滑块区域也能继续跟手。合成事件（自动化测试）没有真实 pointerId，
  // 捕获失败不影响拖动本身，所以这里吞掉异常
  try {
    event.target.setPointerCapture?.(event.pointerId);
  } catch {}
  startedAt.value = Date.now();
  points.value = 0;
  dragging.value = true;
  err.value = '';
  if (phase.value === 'fail') {
    phase.value = 'idle';
    x.value = 0;
  }
};

const onMove = (event) => {
  if (!dragging.value) return;
  const track = trackRef.value;
  if (!track) return;
  const left = track.getBoundingClientRect().left;
  const next = event.clientX - left - HANDLE / 2 - 4;
  x.value = Math.max(0, Math.min(maxX.value, next));
  points.value += 1;
};

const onUp = () => {
  if (!dragging.value) return;
  dragging.value = false;
  if (x.value >= maxX.value * 0.92) {
    x.value = maxX.value;
    finish();
  } else {
    x.value = 0;
  }
};

const pct = computed(() => (maxX.value > 0 ? Math.min(100, (x.value / maxX.value) * 100) : 0));

const label = computed(() => {
  if (phase.value === 'loading') return '正在准备验证…';
  if (phase.value === 'checking') return '校验中…';
  if (phase.value === 'fail') return err.value || '验证未通过，请重试';
  return '按住滑块，拖动到最右边';
});

onMounted(() => {
  measure();
  loadChallenge();
  window.addEventListener('resize', measure);
  setTimeout(measure, 100);
});

onUnmounted(() => window.removeEventListener('resize', measure));
</script>

<style scoped>
.gate {
  display: grid;
  place-items: center;
  min-height: 100vh;
  padding: 24px;
  background:
    radial-gradient(1100px 520px at 50% -10%, #dbeafe, transparent 62%),
    #f8fafc;
}
.gate-card {
  width: 100%;
  max-width: 460px;
  padding: 30px 30px 24px;
  border-radius: 18px;
  background: #fff;
  border: 1px solid #e2e8f0;
  box-shadow: 0 18px 40px rgba(15, 23, 42, .12);
  animation: gate-in .4s cubic-bezier(.2,.75,.2,1);
}
@keyframes gate-in {
  from { opacity: 0; transform: translateY(14px); }
  to   { opacity: 1; transform: translateY(0); }
}
.gate-brand { display: flex; align-items: center; gap: 14px; margin-bottom: 20px; }
.brand-mark {
  width: 44px; height: 44px; flex: 0 0 44px;
  display: grid; place-items: center;
  border-radius: 13px;
  background: #2563eb; color: #fff;
  font-size: 19px; font-weight: 700;
}
.gate-brand h1 { font-size: 18px; font-weight: 650; color: #0f172a; }
.gate-brand p  { margin-top: 5px; font-size: 12px; color: #94a3b8; }
.gate-desc { font-size: 13px; line-height: 1.7; color: #475569; margin-bottom: 20px; }
.gate-desc strong { color: #2563eb; font-weight: 640; }

.slider {
  position: relative;
  height: 52px;
  border-radius: 980px;
  background: #f1f5f9;
  border: 1px solid #e2e8f0;
  overflow: hidden;
  user-select: none;
  transition: border-color .2s, background .2s;
}
.slider.is-dragging { border-color: #2563eb; }
.slider.is-done     { background: #dcfce7; border-color: #22c55e; }
.slider.is-fail     { background: #fee2e2; border-color: #dc2626; }
.slider-fill {
  position: absolute;
  inset: 0 auto 0 0;
  background: linear-gradient(90deg, #dbeafe, #2563eb);
  opacity: .9;
}
.slider.is-done .slider-fill { background: #22c55e; }
.slider.is-fail .slider-fill { background: #dc2626; }
.slider-text {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  font-size: 13px;
  font-weight: 560;
  color: #64748b;
  pointer-events: none;
  transition: opacity .15s linear;
  padding: 0 54px;
  text-align: center;
}
.slider-handle {
  position: absolute;
  top: 4px; left: 4px;
  width: 44px; height: 44px;
  display: grid; place-items: center;
  border-radius: 50%;
  background: #fff;
  color: #2563eb;
  box-shadow: 0 2px 6px rgba(15, 23, 42, .18);
  cursor: grab;
  touch-action: none;
  z-index: 2;
}
.slider-handle:active { cursor: grabbing; }
.slider.is-done .slider-handle { color: #16a34a; }
.slider.is-fail .slider-handle { color: #dc2626; }
.slider-handle:focus-visible { outline: 2px solid #2563eb; outline-offset: 3px; }
.spin { animation: gate-spin .9s linear infinite; }
@keyframes gate-spin { to { transform: rotate(360deg); } }

.gate-err {
  margin-top: 12px;
  padding: 9px 13px;
  border-radius: 8px;
  background: #fee2e2;
  color: #dc2626;
  font-size: 12.5px;
}
.gate-foot {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 16px;
  font-size: 11.5px;
  color: #94a3b8;
}
.gate-foot svg { flex-shrink: 0; }

@media (max-width: 480px) {
  .gate-card { padding: 24px 20px 20px; }
  .gate-brand h1 { font-size: 17px; }
}
</style>
