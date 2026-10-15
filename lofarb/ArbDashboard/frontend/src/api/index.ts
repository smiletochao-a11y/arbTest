/**
 * API 封装层统一导出
 */
export { default as client } from './client'

export * from './fundApi'
export * from './systemApi'
export * from './configApi'

// [AI-2026-10-09 东哥拍板] 删除 './tradingApi' 与 './ledgerApi' 两行转发：
//   该两文件在 LOFarb 侧已是事实孤儿（唯一调用方 useOrderLogic.js 已删；
//   Analysis.vue / AutoTrade.vue / Ledger.vue 页面也已搬到 LOFTrader）。
//   交易/账本能力统一归 LOFTrader(:5174) 自有的一份；LOFarb 不再保留 API 层。
//   参见 memory/2026-10-09.md「17:48 清 LOFarb 事实孤儿」条目。
