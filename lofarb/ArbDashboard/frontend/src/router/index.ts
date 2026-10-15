import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '../layouts/MainLayout.vue'
import Dashboard from '../views/Dashboard.vue'

/**
 * 🔴 [AI-2026-10-08 东哥拍板] 「实时估值计算器」（/lazymode）已整页退役。
 *   该页的持仓实时估值表 / 折溢价分时图 / 盘口+开平仓测算 / 手动下单区
 *   **全部由 LOFTrader(:5174 → :8001) 承接**，LOFarb 只保留数据与看板权威。
 *   ⇒ 交易类操作一律在 LOFTrader 做，这里不再挂这条路由。
 *   历史包袱：`lazyModePath` / `LazyMode` 的「private/ 不存在则降级锁页」机制一并移除。
 */

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: MainLayout,
      redirect: '/dashboard',
      children: [
        {
          path: 'dashboard',
          name: 'Dashboard',
          component: Dashboard
        },
        {
          // 🔴 [AI-2026-10-09 东哥拍板] 「套利雷达/深度分析」整页（Analysis.vue，含实盘下单执行）
          //    搬到 LOFTrader(:5174) 并独立建菜单。LOFarb 侧源文件删除，
          //    与 auto-trade/ledger 同模式：保留占位兼容跳转，旧书签回主看板，不 404。
          path: 'analysis',
          name: 'Analysis',
          redirect: '/dashboard'
        },
        {
          // 🔴 [AI-2026-10-09 东哥拍板] 「信号监视」/「盘后对账」两页源文件
          //    （AutoTrade.vue / Ledger.vue）已删，能力整体归 LOFTrader(:5174)。
          //    与 lazymode 同模式：保留占位兼容跳转，旧书签/旧链接回主看板，不 404 白屏。
          path: 'auto-trade',
          name: 'AutoTrade',
          redirect: '/dashboard'
        },
        {
          // 同上：盘后对账 → 回主看板
          path: 'ledger',
          name: 'Ledger',
          redirect: '/dashboard'
        },
        {
          // [AI-2026-10-12 重做] 原 /data 数据页（Data.vue）随 10-11 UI 改造归档（views/_deprecated/）。
          //   与 analysis/auto-trade/ledger 同模式：保留占位兼容跳转，旧书签回主看板，不 404。
          //   （10-11 23:46 构建 dist 实测：{path:'data',name:'Data',redirect:'/dashboard'}）
          path: 'data',
          name: 'Data',
          redirect: '/dashboard'
        },
        // [AI-2026-10-12 重做] 原 /settings 系统配置页（Settings.vue）随 10-11 UI 改造归档（views/_deprecated/），
        //   路由整体移除（dist 实测：全量 JS 对 path:'settings' 零命中，无 Settings chunk，菜单「系统配置」项同步删除）。
        {
          path: 'etf-rotation',
          name: 'ETFRotation',
          component: () => import('../views/ETFRotation.vue')
        },
        {
          // 🔴 [AI-2026-10-08] 原 /lazymode（实时估值计算器）路由已删 —— 见文件头说明。
          //    占位保留一条指向主看板的兼容跳转，避免旧书签/旧链接直接 404 报错页。
          path: 'lazymode',
          name: 'LazyMode',
          redirect: '/dashboard'
        },
        {
          // [AI-2026-10-09 东哥拍板] 「后台维护」整页迁至 LOFTrader(:5174)。
          //   本机不再提供该页面；保留一条指向主看板的兼容跳转（同 lazymode 处理），
          //   避免旧书签/旧链接直接落到 404。真实入口请去 LOFTrader 顶栏「后台维护」。
          path: 'maintenance',
          name: 'Maintenance',
          redirect: '/dashboard'
        },
        {
          path: 'developing',
          name: 'Developing',
          component: () => import('../views/Developing.vue')
        },
        {
          path: 'holding-analysis',
          name: 'HoldingAnalysis',
          component: () => import('../views/HoldingAnalysis.vue')
        },
        {
          // [AI-2026-10-10] /penetration 改为「原油穿透」三基金持仓口径页（OilPenetration），
          //   菜单「原油穿透」仍指此页；旧单基金底层资产穿透页挪到 /penetration-detail（query 传递保留）
          path: 'penetration',
          name: 'OilPenetration',
          component: () => import('../views/OilPenetration.vue')
        },
        {
          // [AI-2026-10-10] 旧单基金穿透页（HoldingAnalysis「穿透分析」按钮跳转目标）
          path: 'penetration-detail',
          name: 'Penetration',
          component: () => import('../views/Penetration.vue')
        }
      ]
    }
  ]
})

export default router
