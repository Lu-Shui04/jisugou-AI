// client/vite.config.js
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// 部署到子路径时用 VITE_BASE 指定，例如 VITE_BASE=/jisu/
const base = process.env.VITE_BASE || '/';
const apiTarget = process.env.VITE_DEV_API || 'http://localhost:3000';

export default defineConfig({
  base,
  plugins: [vue()],
  server: {
    port: 5173,
    // 本地开发：前端统一用相对路径 /api，由 vite 代理到后端（不再写死 localhost:3000）
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
});
