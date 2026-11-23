import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

const backendHost = process.env.BACKEND_HOST || '127.0.0.1'
const proxyHost = ['0.0.0.0', '::'].includes(backendHost) ? '127.0.0.1' : backendHost
const backendPort = process.env.BACKEND_PORT || '3018'
const backendTarget = `http://${proxyHost}:${backendPort}`

export default defineConfig({
  plugins: [react()],
  experimental: {
    // 后端将资源挂到带版本的路径；分块预加载和 CSS 字体也须跟随该路径。
    renderBuiltUrl(_filename, { hostType }) {
      if (hostType !== 'html') return { relative: true }
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '0.0.0.0',   // dev.sh / dev.ps1 会用 CLI --host 覆盖
    port: 3011,
    watch: {
      // 原子写（写临时文件再改名）会在 src/ 下短暂生成 `.<name>.<pid>.<uuid>.tmpdir/`
      // 与 `.tmp` 文件。Vite 默认用 Node fs.watch 递归监听 src，一旦在改名/删除的
      // 瞬间监听到这类文件就抛 EBUSY 且**无人捕获**，整个 Vite 进程直接退出
      // （实测 2026-11-06 11:53 编辑 src/lib/api.ts、queryKeys.ts、pages/Data.tsx
      // 期间 FE 崩溃，日志 `EBUSY: resource busy or locked ... Data.tsx.tmp`）。
      // 排除它们：这是构建期噪声，永远不该被当作源文件。
      ignored: ['**/.*.tmpdir/**', '**/.*.tmpdir', '**/*.tmp', '**/.*.tmp'],
    },
    proxy: {
      // dev 时 /api 转发到与启动脚本相同的 FastAPI 地址
      '/api': {
        target: backendTarget,
        // SSE 端点需要禁用缓冲
        configure: (proxy) => {
          proxy.on('proxyReq', (_proxyReq, req) => {
            if (req.url?.includes('/stream')) {
              _proxyReq.setHeader('Accept', 'text/event-stream')
              _proxyReq.setHeader('Cache-Control', 'no-cache')
              _proxyReq.setHeader('Connection', 'keep-alive')
            }
          })
        },
      },
      '/health': backendTarget,
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        // 把重型图表库拆到独立 chunk, 避免打进主包 + 让页面按需加载。
        // 用函数形式按 node_modules 路径匹配, 比对象形式更可靠。
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('echarts')) return 'echarts'
            if (id.includes('lightweight-charts')) return 'lightweight-charts'
          }
        },
      },
    },
  },
})
