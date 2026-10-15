<template>
  <!--
    持仓实时价表（2026-10-08 抽取为共享组件）
    原为 ValuationCalculator.vue 内联 <table class="rt-hold-table">，
    因「基金详情」弹窗（FundHistoryModal）要展示**完全同款**表格，
    复制会漂移（列/配色/字号/间距），故抽成唯一实现，两处共用。
    ⚠️ 数据源统一 = useValuationCalculator().holdingRows（valuation_meta），
       口径由 composable 唯一保证，本组件只负责渲染，不取数。
  -->
  <div v-if="rows.length" class="rt-hold-wrap">
    <div class="rt-hold-head">
      <span>{{ title }}</span>
      <span class="rt-hold-hint">{{ displayHint }}</span>
    </div>
    <table class="rt-hold-table">
      <thead>
        <!-- [AI-2026-10-12 重做] 表头按是否全期货双分支（旧 dist）：
             futures 9 列（昨结算价/当日VWAP(结算价)）；normal 保持原 10 列，仅第 5 列旧表头文案改为「实时成交价」 -->
        <tr v-if="isAllFutures">
          <th>序号</th>
          <th>标的</th>
          <th>市场</th>
          <th class="num">占比</th>
          <th class="num">实时成交价</th>
          <th class="num">盘中涨跌</th>
          <th class="num">昨结算价</th>
          <th class="num">当日VWAP(结算价)</th>
          <th class="num">估值影响</th>
        </tr>
        <tr v-else>
          <th>序号</th>
          <th>标的</th>
          <th>市场</th>
          <th class="num">占比</th>
          <th class="num">实时成交价</th>
          <th class="num">盘中涨跌</th>
          <th class="num">T-1收盘</th>
          <th class="num">T-1涨跌</th>
          <th class="num">T-2收盘</th>
          <th class="num">估值影响</th>
        </tr>
      </thead>
      <tbody>
        <!-- [AI-2026-10-12 重做] 每行按 r.isFutures 分支（旧 dist）：公共 4 列（序号/标的/市场/占比）之后，
             futures 行 5 个 td（实时成交价/盘中涨跌/昨结算价/当日VWAP/估值影响）；normal 行 6 个 td 原样保留 -->
        <tr v-for="(r, i) in rows" :key="r.symbol">
          <td class="idx">{{ i + 1 }}</td>
          <td class="sym">{{ r.symbol }}<span v-if="r.name" class="sym-cn">{{ r.name }}</span></td>
          <td class="mkt"><span class="mkt-chip" :class="r.mktClass">{{ r.market }}</span></td>
          <td class="num">{{ fmtNum(r.weight) }}%</td>
          <template v-if="r.isFutures">
            <td class="num rt">{{ fmtNum(r.rt) }}</td>
            <td class="num b" :style="{ color: chgColor(r.rtChg) }">{{ fmtSigned(r.rtChg) }}</td>
            <td class="num">{{ fmtNum(r.settle) }}</td>
            <td class="num">{{ fmtNum(r.vwap) }}</td>
            <td class="num" :style="{ color: chgColor(r.impact) }">{{ fmtSigned(r.impact, 3) }}</td>
          </template>
          <template v-else>
            <td class="num rt">{{ fmtNum(r.rt) }}</td>
            <td class="num b" :style="{ color: chgColor(r.rtChg) }">{{ fmtSigned(r.rtChg) }}</td>
            <td class="num">{{ fmtNum(r.t1) }}</td>
            <td class="num" :style="{ color: chgColor(r.t1Chg) }">{{ fmtSigned(r.t1Chg) }}</td>
            <td class="num">{{ fmtNum(r.t2) }}</td>
            <td class="num" :style="{ color: chgColor(r.impact) }">{{ fmtSigned(r.impact, 3) }}</td>
          </template>
        </tr>
      </tbody>
    </table>
  </div>
  <div v-else-if="showEmpty" class="rt-hold-empty">{{ emptyText }}</div>
</template>

<script setup lang="ts">
// [AI-2026-10-12 重做] computed 用于全期货持仓时的表头/hint 双分支（旧 dist 形态）
import { computed } from 'vue'
/** 持仓实时价表：只渲染，不取数。取数一律由调用方用 useValuationCalculator().holdingRows 传入。 */
const props = withDefaults(defineProps<{
  rows: any[]
  /** 区块标题（弹窗内标题层级不同，可覆盖） */
  title?: string
  /** 标题右侧的算法说明 */
  hint?: string
  /** 无数据时是否显示灰字提示 */
  showEmpty?: boolean
  emptyText?: string
}>(), {
  title: '📋 持仓实时价',
  // [AI-2026-10-12 重做] 默认 hint 分子「实时价」→「实时成交价」（旧 dist 逐字）
  hint: '盘中涨跌 = 实时成交价 ÷ T-1收盘 − 1 · 估值影响 = 占比 × 盘中涨跌',
  showEmpty: false,
  emptyText: '该基金无持仓明细数据（非季报持仓法基金）',
})

// [AI-2026-10-12 重做] 期货持仓专用算法说明（旧 dist 逐字：分子实时成交价、分母昨结算价）
const HINT_FUTURES = '盘中涨跌 = 实时成交价 ÷ 昨结算价 − 1 · 估值影响 = 占比 × 盘中涨跌'
// 全期货持仓（rows 非空且每行 isFutures）→ 表头/hint 走期货口径；否则维持默认
const isAllFutures = computed(() => props.rows.length > 0 && props.rows.every(r => r.isFutures))
const displayHint = computed(() => isAllFutures.value ? HINT_FUTURES : props.hint)

const fmtNum = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? '-' : v.toFixed(digits)
const fmtSigned = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? '-' : (v > 0 ? '+' : '') + v.toFixed(digits) + '%'
// A股惯例：涨红跌绿；0/无数据显示中性灰（与 ValuationCalculator.chgColor 同口径）
const chgColor = (v: number | null | undefined) =>
  v === null || v === undefined || v === 0 ? 'var(--co-neutral)' : v > 0 ? 'var(--co-up)' : 'var(--co-down)'
</script>

<style scoped>
/* 持仓实时价表：紧凑 12px，数字右对齐等宽，与静态弹窗「估值明细」同视觉语言 */
.rt-hold-wrap {
  border-top: 1px dashed var(--co-border-soft);
  padding-top: 6px;
  margin-top: 6px;
}
.rt-hold-head {
  font-size: 12px;
  font-weight: bold;
  color: var(--co-text-3);
  margin-bottom: 4px;
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.rt-hold-hint {
  font-weight: normal;
  font-size: 11px;
  color: var(--co-text-3);
}
.rt-hold-empty {
  padding: 20px;
  color: #94a3b8;
  font-size: 13px;
}
.rt-hold-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--co-fs-sm);
  font-family: var(--co-font-sans);
}
.rt-hold-table th,
.rt-hold-table td {
  padding: 3px 8px;
  border-bottom: 1px solid var(--co-border-soft);
  white-space: nowrap;
}
.rt-hold-table thead th {
  background: var(--co-accent-soft);
  color: var(--co-accent-text);
  font-weight: 600;
  text-align: right;
}
.rt-hold-table thead th:first-child,
.rt-hold-table thead th:nth-child(2),
.rt-hold-table thead th:nth-child(3) {
  text-align: left;
}
.rt-hold-table td.num {
  font-family: var(--co-font-mono);
  text-align: right;
  color: var(--co-text);
}
.rt-hold-table td.b {
  font-weight: bold;
}
.rt-hold-table td.sym {
  font-weight: 700;
  color: var(--co-text);
  text-align: left;
}
.rt-hold-table td.idx {
  color: var(--co-text-3);
  font-family: var(--co-font-mono);
  text-align: left;
  width: 34px;
}
.rt-hold-table td.sym .sym-cn {
  font-weight: normal;
  font-size: 11px;
  color: var(--co-text-3);
  margin-left: 5px;
}
.rt-hold-table td.mkt {
  text-align: left;
}
/* 市场分类配色（淡雅同色系：浅底 + 深字，纯自起色板，互不撞色） */
.rt-hold-table .mkt-chip {
  display: inline-block;
  font-size: 10px;
  line-height: 16px;
  padding: 0 7px;
  border-radius: 8px;
}
.rt-hold-table .mkt-chip.mkt-us    { background: #e8f0fb; color: #2f5d9e; }  /* 美股 · 蓝 */
.rt-hold-table .mkt-chip.mkt-hk    { background: #e3f4f1; color: #1f7a6e; }  /* 香港 · 青 */
.rt-hold-table .mkt-chip.mkt-jp    { background: #fbe9ee; color: #9e3b59; }  /* 日本 · 玫 */
.rt-hold-table .mkt-chip.mkt-eu    { background: #eee9f8; color: #5b3f9e; }  /* 欧洲 · 紫 */
.rt-hold-table .mkt-chip.mkt-cn    { background: #f6f0e0; color: #8a6d1f; }  /* A股 · 琥 */
.rt-hold-table .mkt-chip.mkt-ft    { background: #eceff2; color: #4a5560; }  /* 期货 · 灰 */
.rt-hold-table .mkt-chip.mkt-other { background: var(--co-accent-soft); color: var(--co-accent-text); }
.rt-hold-table td.rt {
  font-weight: bold;
  color: var(--co-accent-text);
}
.rt-hold-table tbody tr:hover {
  background: var(--co-surface-2);
}
</style>
