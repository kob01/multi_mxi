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
];

export default createRouter({
  history: createWebHistory(),
  routes,
});
