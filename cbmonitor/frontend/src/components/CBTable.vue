<template>
  <div class="table-wrapper">
    <div v-if="loading" class="loading">加载中...</div>
    <div v-else-if="bonds.length === 0" class="empty">暂无数据</div>
    <table v-else class="cb-table">
      <thead>
        <tr>
          <th>序号</th>
          <th>代码</th>
          <th>转债名称</th>
          <th class="sortable" @click="toggleSort('bond_price')">现价 <span class="arrow">{{ sortArrow('bond_price') }}</span></th>
          <th class="sortable" @click="toggleSort('bond_change_pct')">涨跌幅 <span class="arrow">{{ sortArrow('bond_change_pct') }}</span></th>
          <th>正股名称</th>
          <th class="sortable" @click="toggleSort('stock_price')">正股价 <span class="arrow">{{ sortArrow('stock_price') }}</span></th>
          <th class="sortable" @click="toggleSort('stock_change_pct')">正股涨跌 <span class="arrow">{{ sortArrow('stock_change_pct') }}</span></th>
          <th class="sortable" @click="toggleSort('convert_price')">转股价 <span class="arrow">{{ sortArrow('convert_price') }}</span></th>
          <th class="sortable" @click="toggleSort('convert_value')">转股价值 <span class="arrow">{{ sortArrow('convert_value') }}</span></th>
          <th class="sortable hl-col" @click="toggleSort('premium_rate')">转股溢价率 <span class="arrow">{{ sortArrow('premium_rate') }}</span></th>
          <th class="sortable" @click="toggleSort('double_low')">双低 <span class="arrow">{{ sortArrow('double_low') }}</span></th>
          <th class="sortable" @click="toggleSort('pure_bond_value')">纯债价值 <span class="arrow">{{ sortArrow('pure_bond_value') }}</span></th>
          <th>评级</th>
          <th class="sortable hl-col" @click="toggleSort('pre_tax_yield')">税前收益率 <span class="arrow">{{ sortArrow('pre_tax_yield') }}</span></th>
          <th class="sortable hl-col" @click="toggleSort('pre_tax_put_yield')">税前回售收益 <span class="arrow">{{ sortArrow('pre_tax_put_yield') }}</span></th>
          <th class="sortable" @click="toggleSort('put_value')">回售价值 <span class="arrow">{{ sortArrow('put_value') }}</span></th>
          <th class="sortable" @click="toggleSort('elasticity')">弹性 <span class="arrow">{{ sortArrow('elasticity') }}</span></th>
          <th class="sortable" @click="toggleSort('pb_ratio')">P/B <span class="arrow">{{ sortArrow('pb_ratio') }}</span></th>
          <th class="sortable" @click="toggleSort('remaining_years')">剩余年限 <span class="arrow">{{ sortArrow('remaining_years') }}</span></th>
          <th>到期时间</th>
          <th class="sortable" @click="toggleSort('remaining_size')">剩余规模 <span class="arrow">{{ sortArrow('remaining_size') }}</span></th>
          <th class="sortable" @click="toggleSort('turnover_rate')">换手率 <span class="arrow">{{ sortArrow('turnover_rate') }}</span></th>
          <th class="sortable" @click="toggleSort('turnover_amount')">成交额(万) <span class="arrow">{{ sortArrow('turnover_amount') }}</span></th>
          <th class="sortable" @click="toggleSort('redeem_trigger_price')">强赎触发价 <span class="arrow">{{ sortArrow('redeem_trigger_price') }}</span></th>
          <th class="sortable" @click="toggleSort('put_trigger_price')">回售触发价 <span class="arrow">{{ sortArrow('put_trigger_price') }}</span></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(b, i) in sortedBonds" :key="b.bond_code" class="cb-row">
          <td class="num">{{ i + 1 }}</td>
          <td class="link">{{ b.bond_code }}</td>
          <td class="name">{{ b.bond_name }}</td>
          <td class="num">{{ fmt3(b.bond_price) }}</td>
          <td class="num" :class="pctClass(b.bond_change_pct)">{{ fmtPct(b.bond_change_pct) }}</td>
          <td class="name">{{ b.stock_name }}</td>
          <td class="num">{{ fmt(b.stock_price) }}</td>
          <td class="num" :class="pctClass(b.stock_change_pct)">{{ fmtPct(b.stock_change_pct) }}</td>
          <td class="num">{{ fmt(b.convert_price) }}</td>
          <td class="num">{{ fmt(b.convert_value) }}</td>
          <td class="num hl-cell">{{ fmtPct(b.premium_rate) }}</td>
          <td class="num">{{ fmt(b.double_low) }}</td>
          <td class="num">{{ fmt(b.pure_bond_value) }}</td>
          <td>{{ b.credit_rating }}</td>
          <td class="num hl-cell">{{ fmtPct(b.pre_tax_yield) }}</td>
          <td class="num hl-cell">{{ fmtPct(b.pre_tax_put_yield) }}</td>
          <td class="num">{{ fmt(b.put_value) }}</td>
          <td class="num">{{ fmt(b.elasticity) }}</td>
          <td class="num">{{ fmt(b.pb_ratio) }}</td>
          <td class="num">{{ b.remaining_years.toFixed(2) }}年</td>
          <td>{{ fmtDate(b.maturity_date) }}</td>
          <td class="num">{{ fmt(b.remaining_size) }}亿</td>
          <td class="num">{{ fmtPct(b.turnover_rate) }}</td>
          <td class="num">{{ fmt(b.turnover_amount) }}</td>
          <td class="num">{{ fmt(b.redeem_trigger_price) }}</td>
          <td class="num">{{ fmt(b.put_trigger_price) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import type { BondVO } from '../api'

const props = defineProps<{
  bonds: BondVO[]
  loading?: boolean
}>()

// 排序状态
const sortKey = ref<string>('')
const sortDir = ref<'asc' | 'desc'>('asc')

function toggleSort(key: string) {
  if (sortKey.value === key) {
    sortDir.value = sortDir.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortKey.value = key
    sortDir.value = 'asc'
  }
}

function sortArrow(key: string): string {
  if (sortKey.value !== key) return ''
  return sortDir.value === 'asc' ? '↑' : '↓'
}

const sortedBonds = computed(() => {
  if (!sortKey.value) return props.bonds
  const key = sortKey.value as keyof BondVO
  const dir = sortDir.value === 'asc' ? 1 : -1
  return [...props.bonds].sort((a, b) => {
    const va = (a[key] as number) || 0
    const vb = (b[key] as number) || 0
    return (va - vb) * dir
  })
})

function fmt(v: number | undefined): string {
  if (!v) return '-'
  return v.toFixed(2)
}
function fmt3(v: number | undefined): string {
  if (!v) return '-'
  return v.toFixed(3)
}
function fmtPct(v: number | undefined): string {
  if (v === undefined || v === 0) return '-'
  return v.toFixed(2) + '%'
}
function pctClass(v: number | undefined): string {
  if (!v) return ''
  return v > 0 ? 'up' : 'down'
}
function fmtDate(d: string | undefined): string {
  if (!d) return '-'
  const m = d.match(/(\d{4})-(\d{2})-(\d{2})/)
  if (m) return m[1].slice(2) + '-' + m[2] + '-' + m[3]
  return d
}
</script>

<style scoped>
.table-wrapper { width: 100%; }
.loading, .empty { text-align: center; padding: 40px; color: #999; }

.cb-table {
  width: 100%; border-collapse: collapse; font-size: 12px;
  table-layout: auto;
  /* [2026-09-27 手机适配] 26 列在窄屏会被压成"一列一个字"的竖排。
     设最小宽度 + 禁止换行，让表格保持可读宽度；横向滚动交给 .table-area
     （它已有 overflow:auto）。⚠️ 绝不能在中间层加 overflow-x:auto——
     会把 overflow-y 隐式变成 auto、破坏 sticky 表头，见 docs/004 §3.5。 */
  min-width: 1500px;
  white-space: nowrap;
}
.cb-table thead th {
  background: #e8f4f8; color: #4a6884; font-weight: 600;
  padding: 6px 4px; text-align: center; border-bottom: 2px solid #d0dde6;
  position: sticky; top: 0; z-index: 10;
}
.cb-table tbody td {
  padding: 5px 4px; text-align: center; border-bottom: 1px solid #f0f0f0;
  color: #333;
}
.cb-row { transition: background 0.15s; }
.cb-row:hover { background: #f0f7ff; }

/* 可排序列头 */
.sortable { cursor: pointer; user-select: none; }
.sortable:hover { background: #d4e8f0; }
.arrow { color: #4a90d9; font-size: 11px; }

.num { text-align: right; font-variant-numeric: tabular-nums; }
.name { text-align: left; }
.link { color: #4a90d9; cursor: pointer; }
.link:hover { text-decoration: underline; }

.up { color: #e74c3c !important; }
.down { color: #27ae60 !important; }

.hl-col { background: #fff9e6; }
.hl-cell { background: #fffbf0; }

/* ============ 手机适配（2026-09-27）============
   26 列在窄屏必然要横向滚动。但滚到"转股溢价率/双低"这些策略指标时，
   如果连"这是哪只债"都看不到，表就没法用了 —— 所以冻结左侧的
   「代码 + 名称」两列（仿 Excel 冻结窗格）。
   序号列信息量最低，窄屏直接隐藏，把宽度让给指标列。
   桌面端（>640px）不受影响，仍显示全部 26 列。

   ⚠️ 冻结列必须自带不透明背景，否则横向滚动的普通列会从它下面透出来。
   ⚠️ 冻结的 th 的 z-index 必须高于普通 th（10），否则被普通表头盖住。 */
@media (max-width: 640px) {
  /* 1) 隐藏序号列 */
  .cb-table th:nth-child(1),
  .cb-table td:nth-child(1) { display: none; }

  /* 2) 冻结代码列（第2列）在 left:0。固定宽度保证第3列的 left 偏移有确定值 */
  .cb-table th:nth-child(2),
  .cb-table td:nth-child(2) {
    position: sticky; left: 0; z-index: 5;
    width: 58px; min-width: 58px;
    background: #fff;
  }
  /* 3) 冻结名称列（第3列），紧贴代码列右侧（58px = 代码列宽） */
  .cb-table th:nth-child(3),
  .cb-table td:nth-child(3) {
    position: sticky; left: 58px; z-index: 5;
    min-width: 86px;
    background: #fff;
    /* 右缘：一道细分隔线 + 柔和投影。投影是必须的 —— 横滚时被冻结列盖住的
       普通列会在边界处露出残片，加投影后残片看起来"在冻结列下层"，视觉自然 */
    box-shadow: 1px 0 0 #e6eef4, 3px 0 6px -2px rgba(0, 0, 0, 0.14);
  }
  /* 3b) 冻结列右缘外侧的渐隐遮罩：把被裁列的半截字符淡出。
       没有它的话，边界上会孤零零露出一个"3"，看起来像数据的一部分、容易误读。
       ::after 定位在冻结列右缘外侧 14px —— td 本身是 sticky（已定位），
       所以 left:100% 相对它计算即可。 */
  .cb-table th:nth-child(3)::after,
  .cb-table td:nth-child(3)::after {
    content: '';
    position: absolute;
    left: 100%; top: 0; bottom: 0;
    width: 14px;
    pointer-events: none;
  }
  .cb-table thead th:nth-child(3)::after {
    background: linear-gradient(to right, rgba(232, 244, 248, 0.95), rgba(232, 244, 248, 0));
  }
  .cb-table tbody td:nth-child(3)::after {
    background: linear-gradient(to right, rgba(255, 255, 255, 0.95), rgba(255, 255, 255, 0));
  }
  .cb-row:hover td:nth-child(3)::after {
    background: linear-gradient(to right, rgba(240, 247, 255, 0.95), rgba(240, 247, 255, 0));
  }
  /* 4) 冻结的表头要盖住横向滚过来的普通表头 */
  .cb-table thead th:nth-child(2),
  .cb-table thead th:nth-child(3) {
    z-index: 15;
    background: #e8f4f8;
  }
  /* 5) 行 hover 时冻结列跟着变色（td 自带白底会盖住 tr 的 hover 色） */
  .cb-row:hover td:nth-child(2),
  .cb-row:hover td:nth-child(3) { background: #f0f7ff; }
}
</style>
