// client/src/router/index.js  （版本检测验证：改动这行注释即产生新的 JS 名字）
import { createRouter, createWebHistory } from 'vue-router';
import ChatView  from '../views/ChatView.vue';
import AgentView from '../views/AgentView.vue';
import RagView   from '../views/RagView.vue';
import GraphView from '../views/GraphView.vue';
import AdminView from '../views/AdminView.vue';

const routes = [
  // 默认落地页 = 基础对话
  { path: '/',       component: ChatView,  meta: { title: '基础对话' } },
  // /chat 保留一条重定向：之前有过 /chat 这个地址，老链接/收藏夹不会打不开
  { path: '/chat',   redirect: '/' },
  { path: '/agent',  component: AgentView, meta: { title: 'Agent 订单查询' } },
  { path: '/rag',    component: RagView,   meta: { title: '知识库问答' } },
  { path: '/graph',  component: GraphView, meta: { title: '多 Agent 中枢' } },
  // 管理员后台：页面内自行校验令牌，未登录时展示登录表单
  { path: '/admin',  component: AdminView, meta: { title: '管理员后台' } },
  // 兜底：收藏夹里的老地址（/index.html、手误路径）也回首页，不出现空白
  { path: '/:pathMatch(.*)*', redirect: '/' },
];

export default createRouter({
  // 跟随构建时的 base（本地是 '/'，部署到子路径时是 '/jisu/'）
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
});
