<template>
  <n-tag
    :type="tagType"
    size="small"
    round
    :bordered="false"
    class="premium-tag"
  >
    {{ displayText }}
  </n-tag>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { formatPremium } from '../../utils'
import { NTag } from 'naive-ui'

const props = withDefaults(defineProps<{
  /** 溢价率数值（如 0.5 表示 0.5%） */
  value?: number | null
  /** 显示的文本，如果不传则自动格式化 value */
  text?: string
}>(), {
  value: null,
  text: ''
})

const tagType = computed(() => {
  if (props.value === null || props.value === undefined) return 'default'
  return props.value > 0 ? 'error' : 'success'
})

const displayText = computed(() => {
  if (props.text) return props.text
  if (props.value === null || props.value === undefined) return '-'
  return formatPremium(props.value)
})
</script>

<style scoped>
.premium-tag {
  font-weight: 700;
  /* [AI-2026-10-12 重做 统一字体/表头] 溢价标签统一引用 mono 字体 token */
  font-family: var(--co-font-mono);
}
</style>
