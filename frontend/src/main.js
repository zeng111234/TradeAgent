import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'
// Order matters: tames Element Plus through its `--el-*` custom properties, so
// it must load *after* `element-plus/dist/index.css` above (a bare `:root`
// override has the same specificity as Element's, and the later one wins) and
// before global.css, which builds on the tokens. Moving these lines around
// silently reverts the whole theme to Element's defaults while every test
// still passes -- that is why the check is a computed-style assertion, not a
// visual inspection.
import './styles/tokens.css'
import './styles/global.css'

const app = createApp(App)

// Register all Element Plus icons
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(ElementPlus, { size: 'default' })
app.use(router)
app.mount('#app')