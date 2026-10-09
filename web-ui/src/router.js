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
];

export default createRouter({
  history: createWebHistory(),
  routes,
});
