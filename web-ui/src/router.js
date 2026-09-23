import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', name: 'chat', component: () => import('./views/ChatView.vue') },
  { path: '/upload', name: 'upload', component: () => import('./views/UploadView.vue') },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
