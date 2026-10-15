<template>
  <n-layout has-sider position="absolute" style="height: 100vh">
    <n-layout-sider
      bordered
      collapse-mode="width"
      :collapsed-width="64"
      :width="150"
      :collapsed="collapsed"
      show-trigger
      @collapse="collapsed = true"
      @expand="collapsed = false"
      style="background-color: #ffffff; display: flex; flex-direction: column;"
    >
      <div class="logo">
        <n-avatar 
          round 
          :size="32" 
          :color="'var(--primary-color)'" 
          style="font-weight: bold; color: white;"
        >
          ARB
        </n-avatar>
        <span v-if="!collapsed" class="logo-text">ArbTest</span>
      </div>
      
      <div style="flex-grow: 1; overflow-y: auto;">
        <n-menu
          v-model:value="activeKey"
          :collapsed="collapsed"
          :collapsed-width="64"
          :collapsed-icon-size="22"
          :options="menuOptions"
          :theme-overrides="menuThemeOverrides"
          class="custom-menu"
        />
      </div>

      <div class="sidebar-footer">
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 4px;">
          <n-tag :type="hasTdx ? 'success' : 'warning'" size="small" round
            :style="{ fontWeight: 'bold', cursor: hasTdx ? 'default' : 'pointer', width: '100%', justifyContent: 'center' }"
            @click="reconnectWithGuard(hasTdx, '通达信', reconnectTdx)">
            <template #icon><n-icon><Zap /></n-icon></template>
            通达信
          </n-tag>
          <n-tag :type="hasIb ? 'success' : 'warning'" size="small" round
            :style="{ fontWeight: 'bold', cursor: 'pointer', width: '100%', justifyContent: 'center' }"
            @click="reconnectIB()">
            <template #icon><n-icon><Zap /></n-icon></template>
            IB
          </n-tag>
          <n-tag :type="hasGalaxy ? 'success' : 'warning'" size="small" round
            :style="{ fontWeight: 'bold', cursor: hasGalaxy ? 'default' : 'pointer', width: '100%', justifyContent: 'center' }"
            @click="reconnectWithGuard(hasGalaxy, '银河QMT', reconnectGalaxy)">
            <template #icon><n-icon><Zap /></n-icon></template>
            银河QMT
          </n-tag>
           <n-tag :type="hasFutu ? 'success' : 'warning'" size="small" round
              :style="{ fontWeight: 'bold', cursor: 'pointer', width: '100%', justifyContent: 'center' }"
              @click="reconnectFutu()">
              <template #icon><n-icon><Zap /></n-icon></template>
              富途
           </n-tag>
           <n-tag :type="hasGuojin ? 'success' : 'warning'" size="small" round
             :style="{ fontWeight: 'bold', cursor: hasGuojin ? 'default' : 'pointer', width: '100%', justifyContent: 'center' }"
             @click="reconnectWithGuard(hasGuojin, '国金QMT', reconnectGuojin)">
             <template #icon><n-icon><Zap /></n-icon></template>
             国金QMT
           </n-tag>
         </div>
         <!-- [AI-2026-10-06 二轮·东哥定调] 系统里程碑日志从主看板顶部移到侧栏按钮下方（券商"待连接"提示文字删除） -->
         <div v-if="!collapsed" class="side-log">
           <div v-for="(m, i) in sidebarMilestones" :key="i" class="side-log-item">
             <span class="side-log-time">{{ m.time }}</span>
             <span class="side-log-msg" :class="(m.level || 'info').toLowerCase()">{{ m.message }}</span>
           </div>
           <div v-if="sidebarMilestones.length === 0" class="side-log-empty">等待系统汇报…</div>
         </div>
      </div>
    </n-layout-sider>
    <n-layout>
      <n-layout-header bordered style="height: 30px; display: flex; align-items: center; justify-content: flex-end; padding: 0 20px; background: var(--co-surface);">
          <div v-if="showDataAlert" class="nav-alert-bar">
            <n-icon size="14" color="#a9790f"><AlertTriangle /></n-icon>
            <span>{{ navAlertText }}</span>
          </div>
        </n-layout-header>
        <n-layout-content content-style="padding: 10px; background-color: var(--co-bg); height: calc(100vh - 30px); overflow: auto;">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </n-layout-content>
    </n-layout>
  </n-layout>
</template>

<script setup lang="ts">
import { ref, h, computed, onMounted, onUnmounted } from 'vue'
import { RouterLink } from 'vue-router'
import { storeToRefs } from 'pinia'
import { 
  NLayout, 
  NLayoutSider, 
  NLayoutHeader, 
  NLayoutContent, 
  NMenu, 
  NAvatar, 
  NIcon,
  NText,
  NTag,
  useMessage
} from 'naive-ui'
import {
  AlertTriangle,
  Zap,
} from 'lucide-vue-next'
import { useAppStore, useMarketStore } from '../store'

const collapsed = ref(false)
const activeKey = ref('dashboard')

// [AI-2026-08-07] 压缩菜单行高：naive-ui 菜单项高度由主题变量 itemHeight 控制(默认42px)，
// 单改 padding 无效(项高固定、内容垂直居中)，必须覆写 itemHeight 才能真正变矮。
const menuThemeOverrides = {
  itemHeight: '32px'
}

const showDataAlert = ref(false)
const navAlertText = ref('')

const message = useMessage()
const appStore = useAppStore()
const marketStore = useMarketStore()

const { hasTdx, hasIb, hasGalaxy, hasGuojin, hasFutu, hasFutuNoData } = storeToRefs(marketStore)



// ===== Reconnect 引擎状态 =====
const refreshStatus = () => marketStore.fetchOverview()

const reconnectIB = async () => {
  appStore.reconnectingIB = true
  try {
    const data = await appStore.reconnectIB()
    if (data.status === 'ok') {
      message.success('IB 重连成功！')
      refreshStatus()
    } else {
      message.error('IB 重连失败，请确保 TWS 已运行。')
    }
  } catch (e: any) {
    message.error('重连请求失败: ' + e.message)
  } finally {
    appStore.reconnectingIB = false
  }
}

const reconnectEngine = async (sourceLabel: string, reconnectFn: () => Promise<any>) => {
  try {
    const data = await reconnectFn()
    if (data.status === 'ok') {
      message.success(`${sourceLabel} 重连成功！`)
      refreshStatus()
    } else if (data.status === 'pending') {
      // [AI-2026-08-20] 异步重连：后端立即返回 pending，连接结果经状态栏轮询体现
      message.info(`${sourceLabel} ${data.message || '重连中，请稍候'}`)
      refreshStatus()
    } else {
      message.warning(`${sourceLabel} 重连未就绪: ${data.message}`)
    }
  } catch (e: any) {
    message.error(`${sourceLabel} 重连异常: ${e.message}`)
  }
}

const reconnectWithGuard = (isConnected: boolean, label: string, fn: () => void) => {
  if (isConnected) return
  fn()
}

const reconnectTdx = () => reconnectEngine('通达信', () => marketStore.reconnectTdx())
const reconnectGalaxy = () => reconnectEngine('银河QMT', () => marketStore.reconnectGalaxy())
const reconnectGuojin = () => reconnectEngine('国金QMT', () => marketStore.reconnectGuojin())
const reconnectFutu = () => reconnectEngine('富途', () => marketStore.reconnectFutu())

// [AI-2026-10-06 二轮] 侧栏系统日志：读 appStore.milestones（本组件 onMounted 自行轮询，不依赖 Dashboard 页）。
// 过滤券商"请点击左侧…"/"如需实时行情"提示（东哥定调删除的废话）。
const { milestones } = storeToRefs(appStore)
const sidebarMilestones = computed(() =>
  (milestones.value || []).filter((m: any) => {
    const msg = String(m.message || '')
    return !msg.includes('请点击左侧') && !msg.includes('如需实时行情')
  })
)

const fetchNavAlert = async () => {
  try {
    const res = await fetch('/api/system/nav-status')
    const data = await res.json()
    if (data.status === 'ok') {
      const todayUpdated = data.data.today_updated
      const lastTime = data.data.last_updated_time
      const now = new Date()
      const hour = now.getHours()
      const isWeekend = now.getDay() === 0 || now.getDay() === 6
      if (!isWeekend && hour >= 15 && !todayUpdated) {
        showDataAlert.value = true
        navAlertText.value = '今日净值尚未补采，部分基金可能显示过期数据'
      } else if (!isWeekend && hour >= 15 && todayUpdated) {
        showDataAlert.value = true
        navAlertText.value = `今日净值已更新 (${lastTime})`
      } else {
        showDataAlert.value = false
      }
    }
  } catch (e) { /* ignore */ }
}

onMounted(() => {
  fetchNavAlert()
  setInterval(fetchNavAlert, 300000)
  // [AI-2026-07-15] 定期刷新数据源连接状态，避免 IB/富途退出后标签仍显示绿色
  marketStore.fetchOverview()
  setInterval(() => marketStore.fetchOverview(), 30000)
  // [AI-2026-10-06 二轮] 侧栏日志数据源：本组件自行轮询系统里程碑，任何页面都保持新鲜
  appStore.fetchSystemStatus()
  setInterval(() => appStore.fetchSystemStatus(), 30000)
})

const menuOptions = [
  {
    label: () => h(RouterLink, { to: '/dashboard' }, { default: () => '套利看板' }),
    key: 'dashboard'
  },
  // [AI-2026-10-09 东哥拍板] 删除「信号监视(auto-trade)」「盘后对账(ledger)」两项侧边栏入口：
  //   两者均为交易台能力（规则引擎 / 套利账本），按目标架构已归属 LOFTrader(:5174)，
  //   不应出现在 LOFarb(数据/估值/看板)。路由与页面保留未删（后端端点仍供 LOFTrader 透传调用），
  //   仅移除 LOFarb 侧边栏暴露的入口，求稳可逆。
  // [AI-2026-10-10] 「原油穿透」菜单项上移到「ETF轮动」之前，顺序 = 套利看板 / 原油穿透 / ETF轮动
  // [AI-2026-10-12 重做] 删除「系统配置」侧边栏入口：Settings.vue 已随 10-11 UI 改造归档
  //   （views/_deprecated/），/settings 路由一并移除（10-11 23:46 dist 实测菜单仅 3 项、系统配置=0）。
  {
    label: () => h(RouterLink, { to: '/penetration' }, { default: () => '原油穿透' }),
    key: 'penetration'
  },
  {
    label: () => h(RouterLink, { to: '/etf-rotation' }, { default: () => 'ETF轮动' }),
    key: 'etf-rotation'
  }
  // [AI-2026-10-09 东哥拍板] 「后台维护」整页迁至 LOFTrader(:5174)：
  //   导出 / 补采 / 重算 属**人工运维交互**，只有本机跑得起来，ARM 展示副本上无意义。
  //   数据侧能力不变 —— 页面仍在 LOFarb :8000，本机只是经 lofarb_proxy 透传调用。
]
</script>

<style scoped>
.logo { height: 46px; display: flex; align-items: center; padding: 0 14px; gap: 10px; }
.logo-text { font-size: 18px; font-weight: 800; background: linear-gradient(120deg, #1d4ed8, #0891b2); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.sidebar-footer { padding: 6px; border-top: 1px solid var(--co-border); background: var(--co-surface-2); }
/* [AI-2026-10-06 东哥需求] 5个券商按键压缩：小字号+紧padding，省宽度给主看板 */
.sidebar-footer :deep(.n-tag) { font-size: 10px; padding: 0 3px; height: 22px; }
.sidebar-footer :deep(.n-tag .n-base-slot) { margin-right: 1px; }
/* [AI-2026-10-06 二轮] 侧栏系统日志（原主看板顶部日志卡移此）：紧凑纵向滚动列表 */
.side-log {
  margin-top: 8px;
  padding: 6px 4px 2px;
  border-top: 1px dashed var(--co-border-soft);
  max-height: 180px;
  overflow-y: auto;
}
.side-log-item {
  display: flex;
  align-items: flex-start;
  gap: 5px;
  font-size: 10px;
  line-height: 1.5;
  padding: 1px 0;
}
.side-log-time {
  color: var(--co-text-3);
  flex-shrink: 0;
  /* [AI-2026-10-12 重做 统一字体/表头] 侧栏日志时间统一引用 mono 字体 token */
  font-family: var(--co-font-mono);
  font-size: 9px;
  padding-top: 1px;
}
.side-log-msg { color: var(--co-text-2); word-break: break-all; text-align: left; }
.side-log-msg.error { color: var(--co-up); font-weight: 600; }
.side-log-msg.warning { color: var(--co-warn); }
.side-log-msg.success { color: var(--co-down); font-weight: 600; }
.side-log-empty { font-size: 10px; color: var(--co-text-3); text-align: center; padding: 6px 0; }
:deep(.n-menu-item-content--selected) { background-color: var(--primary-light) !important; }
:deep(.n-menu-item-content--selected .n-menu-item-content-header a) { color: var(--primary-color) !important; }
:deep(.n-menu-item) { height: 32px !important; }
:deep(.n-menu-item-content) { border-radius: 8px; margin: 0 8px; color: var(--co-text-2); font-weight: 650; }
/* [AI-2026-10-06 三轮·东哥定调] 菜单删图标+缩字体（12px），150px 宽度下四字菜单不再截断 */
:deep(.n-menu .n-menu-item-content-header) { font-size: 12px !important; }
:deep(.n-menu .n-menu-item-content-header a) { font-size: 12px !important; }
.nav-alert-bar {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--co-warn);
  background: var(--co-warn-soft); border: 1px solid var(--co-border);
  padding: 2px 12px; border-radius: 12px;
}
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
