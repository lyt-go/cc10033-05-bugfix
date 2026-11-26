import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import SampleFlowView from '@/views/SampleFlowView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/sample-flow', name: 'sample-flow', component: SampleFlowView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
