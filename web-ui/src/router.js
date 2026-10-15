import { createRouter, createWebHistory } from "vue-router";

const routes = [
  { path: "/", name: "chat", component: () => import("./views/ChatView.vue") },
  {
    path: "/upload",
    name: "upload",
    component: () => import("./views/UploadView.vue"),
  },
  {
    path: "/memory",
    name: "memory",
    component: () => import("./views/MemoryView.vue"),
  },
  {
    path: "/graph",
    name: "graph",
    component: () => import("./views/GraphView.vue"),
  },
  {
    // 数据变更审批台(层 4 的人工审批流): 待办列表 + 变更前镜像 + 审计回查。
    path: "/dataops",
    name: "dataops",
    component: () => import("./views/DataOpsView.vue"),
  },
  {
    // 问数治理看板(运行指标/告警线/工单/样本队列/评测集与语义缓存复用批准)。
    // 入口对谁可见只是体验层: 每个接口都服务端重查角色(能跨全员看数 = 能看全员问数记录)。
    path: "/askdata",
    name: "askdata-ops",
    component: () => import("./views/AskDataOpsView.vue"),
  },
];

export default createRouter({
  history: createWebHistory(),
  routes,
});
