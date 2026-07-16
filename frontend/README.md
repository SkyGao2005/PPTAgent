# PPTAgent Frontend

React + TypeScript + Vite 前端，使用 Tailwind CSS v4、shadcn/ui、Zustand 与 React Router。

## 本地运行

```bash
npm install
npm run dev
```

默认访问 `http://localhost:5173`。开发环境下 `/api` 会代理到
`http://127.0.0.1:8000`；也可以复制 `.env.example` 并通过
`VITE_API_BASE_URL` 指向其他 FastAPI 地址。

- 同源联调：保持 `VITE_API_BASE_URL` 为空，通过 `VITE_DEV_API_TARGET` 修改
  Vite 的 `/api` 代理目标，无需配置浏览器 CORS。
- 跨源联调/生产：设置 `VITE_API_BASE_URL=https://api.example.com`。REST、SSE、
  预览图和导出下载都会使用该地址；后端需允许前端来源访问，并允许下载接口的 CORS。

## 目录

```text
src/
  components/       通用组件及 shadcn/ui 源码
  lib/api.ts        REST 与 SSE 客户端
  mocks/            后端接入前的演示数据
  pages/            创建页、模板库、三栏工作台
  stores/           Zustand 页面状态
  types/api.ts      与后端协议对应的 TypeScript 类型
```

当前页面使用 mock 数据演示完整信息架构。后端就绪后，以 `lib/api.ts` 为入口
替换 mock 数据，不要在页面组件内散落 `fetch` 或解析 Agent 自然语言日志。

## 测试

```bash
npm test
```

使用 Vitest 覆盖 store 层的事件归并、占位页迁移、任务切换竞态与排队编辑逻辑。

## 部署

前端使用 `BrowserRouter`，生产环境必须把所有非 `/api` 路由回写到
`index.html`，否则刷新 `/workbench/:taskId` 等深链接会返回 404。

- Vercel：项目 Root Directory 必须设为 `frontend`，其中的 `vercel.json` 已内置
  SPA rewrite。Vercel 不会自动转发 `/api`，生产部署必须设置
  `VITE_API_BASE_URL`，或在平台上另行配置同源 API rewrite。
- Nginx 示例：

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8000;
}
location / {
    root /srv/pptagent/dist;
    try_files $uri /index.html;
}
```

Node 版本见 `.nvmrc`（`engines` 要求 ≥ 20）。
