import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 构建产物输出到 web/dist，由 FastAPI 直接托管（仅生产/打包）；
// dev 模式页面由本 dev server 提供，API 通过代理转发到后端，不依赖 web/dist 存在。
//
// 代理目标优先级: VITE_API_TARGET > 仓根 .env 的 ASSISTANT_PORT > 18000。
// 后端宿主端口为 18000（Docker 发布端口，避开 Windows winnat 端口排除段，
// 见 docker/docker-compose.yml 顶部约定）；读 .env 而不写死端口，是为了跟
// app/config.py 的 assistant_port 共用一个事实源，两套跑法 URL 不会走歪。
function resolveApiTarget() {
  if (process.env.VITE_API_TARGET) return process.env.VITE_API_TARGET;
  let port = "18000";
  try {
    const envFile = fileURLToPath(new URL("../.env", import.meta.url));
    const hit = readFileSync(envFile, "utf8").match(/^ASSISTANT_PORT=\s*(\d+)\s*$/m);
    if (hit) port = hit[1];
  } catch {
    /* .env 缺失(如容器内构建)则用默认端口, 不阻断前端启动 */
  }
  return `http://127.0.0.1:${port}`;
}

const apiTarget = resolveApiTarget();
// SSE(/api/chat/stream)靠 http-proxy 默认的非缓冲透传即可(无需 ws 升级),
// ws: false 显式声明不走 WebSocket 代理, 避免以后误加 upgrade 配置打断流式响应。
const proxyEntry = {
  target: apiTarget,
  changeOrigin: true,
  ws: false,
};

export default defineConfig({
  plugins: [vue()],
  base: "/",
  build: {
    outDir: "../web/dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": proxyEntry,
      // 健康检查与 API 文档也在 /api 前缀或 FastAPI 自带路由下, 一并代理,
      // 这样开发期统一从 :5173 访问, 不会有人为了看 /docs 去改 URL。
      "/docs": proxyEntry,
      "/redoc": proxyEntry,
      "/openapi.json": proxyEntry,
    },
  },
});
