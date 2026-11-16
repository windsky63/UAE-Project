<script setup>
import ActionIcon from './ActionIcon.vue'
import HelpTip from './HelpTip.vue'
import LogClearButton from './LogClearButton.vue'
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { VPagination } from 'vuetify/components'

const props = defineProps({ api: { type: Function, required: true } })
const emit = defineEmits(['notice'])
const data = ref({ items: [], total: 0 })
const level = ref('')
const tool = ref('')
const search = ref('')
const page = ref(1)
const automatic = ref(true)
const compact = ref(true)
const loading = ref(false)
const failure = ref('')
const clearResult = ref('')
let request = 0, timer, debounce
const levels = [{ title: '全部级别', value: '' }, { title: '信息', value: 'INFO' }, { title: '警告', value: 'WARNING' }, { title: '错误', value: 'ERROR' }, { title: '严重错误', value: 'CRITICAL' }]
const colors = { INFO: 'info', WARNING: 'warning', ERROR: 'error', CRITICAL: 'error' }

async function load() {
  const current = ++request
  loading.value = true
  try {
    const params = new URLSearchParams({ tool:tool.value, level: level.value, search: search.value || '', compact:compact.value?'1':'0', limit: '20', offset: String((page.value - 1) * 20) })
    const result = await props.api(`/logs?${params}`)
    if (current === request) { data.value = result; failure.value = '' }
  } catch (error) {
    if (current === request) failure.value = error.message
  } finally { if (current === request) loading.value = false }
}
function filter() { if (page.value !== 1) page.value = 1; else load() }
function logsCleared(message) {
  clearResult.value = message
  if (page.value !== 1) page.value = 1
  else load()
}
watch(level, filter)
watch(tool, filter)
watch(compact, filter)
watch(search, () => { clearTimeout(debounce); debounce = setTimeout(filter, 300) })
watch(page, load)
onMounted(() => {
  load()
  timer = setInterval(() => { if (automatic.value && page.value === 1 && !loading.value && !document.hidden) load() }, 5000)
})
onUnmounted(() => { request++; clearInterval(timer); clearTimeout(debounce) })
</script>

<template>
  <v-card data-testid="logs-panel">
    <div class="pa-5 d-flex ga-4 flex-wrap align-center">
      <div class="flex-grow-1"><div class="section-title"><h2 class="text-h6">运行日志</h2><HelpTip label="运行日志说明" text="查看同步、上传、微信监听及管理中心发起的任务输出。每5秒刷新首页，读取最新日志的尾部，不扫描完整历史。" /></div></div>
      <v-switch v-model="automatic" label="自动刷新" color="primary" hide-details density="compact" />
      <v-switch v-model="compact" label="合并进度" color="primary" hide-details density="compact" />
      <ActionIcon :loading="loading" @click="load" label="刷新日志" icon="mdi-refresh"/>
      <LogClearButton :api="api" :tool="tool" @notice="(...args) => emit('notice', ...args)" @cleared="logsCleared" />
    </div>
    <div class="log-filters">
      <v-select v-model="tool" :items="[{title:'全部工具',value:''},{title:'FusionLive 同步',value:'sync'},{title:'资料解析及管理工具',value:'analysis'},{title:'绿联云上传',value:'nas'},{title:'微信与 Codex',value:'wechat'},{title:'ima 知识库',value:'ima'}]" label="工具" hide-details />
      <v-select v-model="level" :items="levels" label="日志级别" hide-details />
      <v-text-field v-model="search" label="搜索日志" placeholder="文件名、任务、错误…" prepend-inner-icon="mdi-magnify" clearable @click:clear="search = ''" hide-details />
    </div>

    <v-alert v-if="clearResult" type="info" variant="tonal" class="mx-5 my-4" closable @click:close="clearResult = ''">{{ clearResult }}</v-alert>
    <v-alert v-if="failure" type="error" variant="tonal" class="mx-5 mb-4">{{ failure }}</v-alert>
    <v-progress-linear v-if="loading" indeterminate color="primary" />
    <div v-if="data.items.length" class="px-5 pb-4">
      <article v-for="item in data.items" :key="item.id" class="log-entry py-4">
        <div class="d-flex flex-wrap ga-2 align-center mb-2">
          <v-chip :color="colors[item.level] || 'secondary'" size="small" label>{{ item.level }}</v-chip>
          <time class="text-caption">{{ new Date(item.created_at).toLocaleString('zh-CN', { hour12: false, timeZone:'Asia/Shanghai' }) }}</time>
          <span class="text-caption text-secondary" :title="item.duplicate_sources?.join('\n')">{{ item.source }}</span>
        </div>
        <p class="log-text text-body-2">{{ item.summary || item.message }}</p>
        <details v-if="item.diagnostic" class="mt-3"><summary class="text-body-2">查看完整诊断</summary><pre class="log-details mt-2 pa-3">{{ item.diagnostic }}</pre></details>
        <div v-if="item.progress" class="mt-3" data-testid="log-progress">
          <v-progress-linear :model-value="item.progress.total>0?Math.min(100,100*item.progress.current/item.progress.total):0" :indeterminate="false" color="primary" height="8" rounded :aria-label="item.progress.phase+'进度'" />
          <div class="text-caption text-secondary mt-1">{{ item.progress.phase }} · {{ item.progress.total>0?`${item.progress.current} / ${item.progress.total}`:'未提供总量，显示最近进度记录' }} · 本次读取已合并 {{ item.progress.updates }} 条更新</div>
        </div>
        <template v-if="item.detail">
          <p v-if="item.detail.hint" class="text-body-2 mt-2 text-primary">{{ item.detail.hint }}</p>
          <details class="mt-3"><summary class="text-body-2">查看诊断详情</summary><pre class="log-details mt-2 pa-3">{{ JSON.stringify(item.detail, null, 2) }}</pre></details>
        </template>
      </article>
    </div>
    <p v-else-if="!loading && !failure" class="pa-5 text-secondary">暂无匹配的日志。</p>
    <div class="px-5 pb-3 text-caption text-secondary">本次读取匹配 {{ data.total }} 条<span v-if="data.duplicates_merged"> · 已合并 {{ data.duplicates_merged }} 条跨文件副本</span> · {{ data.scope || '最近日志尾部' }}。关闭“合并进度”可查看逐条记录；原始日志文件保留。</div>
    <v-pagination v-if="data.total > 20" v-model="page" :length="Math.ceil(data.total / 20)" :total-visible="4" page-aria-label="第 {0} 页" current-page-aria-label="当前第 {0} 页" aria-label="日志分页" />

  </v-card>
</template>

<style scoped>
.log-filters{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,2fr);gap:16px;padding:0 20px 20px}.log-filters>.v-input{min-width:0}
@media(max-width:700px){.log-filters{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.log-filters>.v-input:last-child{grid-column:1/-1}}
@media(max-width:400px){.log-filters{grid-template-columns:minmax(0,1fr)}}
.log-entry + .log-entry { border-top: 1px solid rgba(var(--v-theme-on-surface), .1); }
.log-text, .log-details { overflow-wrap: anywhere; white-space: pre-wrap; }
.log-details { background: rgba(var(--v-theme-on-surface), .04); border-radius: 8px; font-size: 12px; }
summary { cursor: pointer; }
</style>
