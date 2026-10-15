<template>
  <n-config-provider :theme-overrides="themeOverrides">
    <n-global-style />
    <n-loading-bar-provider>
      <n-message-provider>
        <n-notification-provider>
          <n-dialog-provider>
            <router-view v-slot="{ Component }">
              <keep-alive>
                <component :is="Component" />
              </keep-alive>
            </router-view>
          </n-dialog-provider>
        </n-notification-provider>
      </n-message-provider>
    </n-loading-bar-provider>
  </n-config-provider>
</template>

<script setup lang="ts">
import { ref, computed, provide, watchEffect } from 'vue'
import {
  NConfigProvider,
  NGlobalStyle,
  NMessageProvider,
  NNotificationProvider,
  NDialogProvider,
  NLoadingBarProvider
} from 'naive-ui'
import type { GlobalThemeOverrides } from 'naive-ui'

// 获取持久化的主题色，默认蓝色
const savedColor = localStorage.getItem('theme_color') || '#3B82F6'
const primaryColor = ref(savedColor)

// 计算浅色背景（用于选中态）
const getLightColor = (hex: string) => {
    return hex + '15'; // 约 8% 透明度的浅色
}

const themeOverrides = computed<GlobalThemeOverrides>(() => ({
  common: {
    primaryColor: primaryColor.value,
    primaryColorHover: '#4f8ef7',
    primaryColorPressed: '#1d4ed8',
    primaryColorSuppl: primaryColor.value,
    bodyColor: '#f7f8fa',
    cardColor: '#ffffff',
    tableColor: '#ffffff',
    textColorBase: '#1f2328',
    borderColor: '#ebedf0',
    // [AI-2026-10-12 重做 统一字体/表头] Naive UI 全局字体接入双字体栈：fontFamily 写与 --co-font-sans 完全一致的
    // 字面栈（Naive 把主题值注入内联 CSS 变量，嵌套 var() 风险高，故不写 var()）；fontSize 统一 13px。
    fontFamily: 'v-sans, "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", system-ui, -apple-system, "Segoe UI", Arial, sans-serif',
    fontSize: '13px'
  },
  DataTable: {
    thFontWeight: '700',
    // [AI-2026-10-12 重做 统一字体/表头] 表头三色统一到参照表 RtHoldingTable 口径：底 #edf3fb / 字 #33629c / 悬停 #e2edf9，
    // 其余键（td 色、border、fontWeight）保持原样不动。
    thColor: '#edf3fb',
    thColorHover: '#e2edf9',
    tdColor: '#ffffff',
    tdColorHover: '#f6f7f9',
    tdColorStriped: '#fbfcfd',
    tdColorHoverStriped: '#f6f7f9',
    borderColor: '#ebedf0',
    thTextColor: '#33629c',
    tdTextColor: '#1f2328'
  },
  // [AI-2026-10-12 重做 统一字体/表头] 新增 Table 主题表头色：覆盖 <n-table>（如 HoldingAnalysis 5 处），
  // 当前回退 naive 默认 #fafafc，此处全局统一为 #edf3fb/#33629c。
  Table: {
    thColor: '#edf3fb',
    thTextColor: '#33629c'
  },
  Card: {
    borderRadius: '8px'
  }
}))

// 动态更新 CSS 变量，确保自定义主题生效
watchEffect(() => {
    document.documentElement.style.setProperty('--primary-color', primaryColor.value);
    document.documentElement.style.setProperty('--primary-light', getLightColor(primaryColor.value));
})

provide('themeColor', primaryColor)
</script>

<style>
:root {
  /* 初始变量 */
  --primary-color: #2563eb;
  --primary-light: #eff6ff;
  --bg-color: #f7f8fa;
  --text-main: #1f2937;
  --text-muted: #64748b;

  /* === LOFarb 统一设计系统 · Codex 淡雅风 (2026-10-07) ===
     实时估值页(ValuationCalculator) + 静态历史弹窗(Dashboard) 统一引用以下 token，
     取代散落的硬编码强色。低饱和、克制、靠边框与留白分隔，不靠高饱和色块。 */
  --co-bg: #f7f8fa;            /* 页面底：中性微冷灰白 */
  --co-surface: #ffffff;       /* 卡片/面板底 */
  --co-surface-2: #f6f7f9;     /* 次级底（信息条/卡内分区） */
  --co-border: #ebedf0;        /* 主边框/分隔线 */
  --co-border-soft: #f1f2f4;   /* 表内细线 */
  --co-text: #1f2328;          /* 主文 */
  --co-text-2: #5b6470;        /* 次文 */
  --co-text-3: #8a929e;       /* 弱文/标签 */
  --co-accent: #5b8fc9;        /* 主强调（淡雅蓝：估值/溢价强调，非涨跌语义不用绿） */
  --co-accent-soft: #edf3fb;   /* 强调底色 */
  --co-accent-text: #33629c;   /* 强调文字（深一档） */
  --co-up: #d64045;            /* 涨红（柔和） */
  --co-down: #2e9e5b;          /* 跌绿（柔和） */
  --co-neutral: #8a929e;       /* 中性（0/无数据） */
  --co-up-soft: #fbeaeb;       /* 涨红·淡底（按钮/提示） */
  --co-down-soft: #e9f5ed;     /* 跌绿·淡底 */
  --co-warn: #a9790f;          /* 警示琥珀（低饱和） */
  --co-warn-soft: #faf4e6;     /* 警示淡底 */
  /* [AI-2026-10-09 东哥拍板] 删除 --co-rt-bg/head、--co-static-bg/head、--co-si-bg/head：
     三组「估值列整列底色」的 CSS 已随之从 Dashboard.vue 移除（残留意图见该文件注释），
     六个变量引用归零 ⇒ 一并删掉，避免留下没人用的调色板。 */
  /* [AI-2026-10-12 重做 统一字体/表头] 全站唯一字体真相源（两套栈）：sans=v-sans(UI 文本)/mono=v-mono(代码数字)，
     中文均经 "Microsoft YaHei UI"/"PingFang SC" 回退。所有组件 font-family 一律 var(--co-font-sans) / var(--co-font-mono)。 */
  --co-font-sans: v-sans, "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", system-ui, -apple-system, "Segoe UI", Arial, sans-serif;
  --co-font-mono: v-mono, "Microsoft YaHei UI", "PingFang SC", Consolas, "Courier New", monospace;
  --co-radius: 8px;
  --co-radius-sm: 6px;
  --co-fs-title: 14px;
  --co-fs-base: 13px;
  --co-fs-sm: 12px;
  --co-fs-xs: 11px;
  --co-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}

body {
  margin: 0;
  padding: 0;
  /* [AI-2026-10-12 重做 统一字体/表头] body 字体统一引用单一真相源 token（font-size 13px 保持不变） */
  font-family: var(--co-font-sans);
  background-color: var(--bg-color);
  color: var(--text-main);
  font-size: 13px;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

html {
  background-color: var(--bg-color);
  color-scheme: light;
}

#app {
  background-color: var(--bg-color);
}

/* 修正后的全站标准按钮样式 */
.btn-standard {
  padding: 6px 16px;
  background-color: var(--primary-light) !important;
  color: var(--primary-color) !important;
  border: 1px solid var(--primary-color) !important;
  border-radius: 6px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
}

/* 修复：悬浮时文字必须变白，背景变深，防止看不清 */
.btn-standard:hover {
  background-color: var(--primary-color) !important;
  color: #ffffff !important; 
}

.btn-standard:active {
  filter: brightness(0.9);
}

.btn-standard:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* ========================================================
   全局覆盖 Naive UI n-button 样式以匹配 .btn-standard 风格
   ======================================================== */
.n-button:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text) {
  transition: all 0.2s !important;
  border-radius: 6px !important;
  font-weight: 600 !important;
}

/* 默认与 primary 按钮 */
.n-button.n-button--default-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text),
.n-button.n-button--primary-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text) {
  background-color: var(--primary-light) !important;
  color: var(--primary-color) !important;
  border: 1px solid var(--primary-color) !important;
}

.n-button.n-button--default-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text):hover,
.n-button.n-button--primary-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text):hover {
  background-color: var(--primary-color) !important;
  color: #ffffff !important;
}

.n-button.n-button--default-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text):hover *,
.n-button.n-button--primary-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text):hover * {
  color: #ffffff !important;
  fill: #ffffff !important;
}

/* success 状态按钮 */
.n-button.n-button--success-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text) {
  background-color: var(--co-down-soft) !important;
  color: var(--co-down) !important;
  border: 1px solid var(--co-down) !important;
}
.n-button.n-button--success-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text):hover {
  background-color: var(--co-down) !important;
  color: #ffffff !important;
}
.n-button.n-button--success-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text):hover * {
  color: #ffffff !important;
}

/* warning 状态按钮 */
.n-button.n-button--warning-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text) {
  background-color: var(--co-warn-soft) !important;
  color: var(--co-warn) !important;
  border: 1px solid var(--co-warn) !important;
}
.n-button.n-button--warning-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text):hover {
  background-color: var(--co-warn) !important;
  color: #ffffff !important;
}
.n-button.n-button--warning-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text):hover * {
  color: #ffffff !important;
}

/* error 状态按钮 */
.n-button.n-button--error-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text) {
  background-color: var(--co-up-soft) !important;
  color: var(--co-up) !important;
  border: 1px solid var(--co-up) !important;
}
.n-button.n-button--error-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text):hover {
  background-color: var(--co-up) !important;
  color: #ffffff !important;
}
.n-button.n-button--error-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text):hover * {
  color: #ffffff !important;
}

/* info 状态按钮 */
.n-button.n-button--info-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text) {
  background-color: var(--co-accent-soft) !important;
  color: var(--co-accent-text) !important;
  border: 1px solid var(--co-accent) !important;
}
.n-button.n-button--info-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text):hover {
  background-color: var(--co-accent) !important;
  color: #ffffff !important;
}
.n-button.n-button--info-type:not(.n-button--quaternary):not(.n-button--circle):not(.n-button--dashed):not(.n-button--text):hover * {
  color: #ffffff !important;
}
</style>
