<script setup>
import HelpTip from './HelpTip.vue'
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
const clearDialog = ref(false)
const clearing = ref(false)
const clearScope = ref('history')
const clearTarget = ref('')
const clearFailure = ref('')
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
function openClear() {
  clearTarget.value = tool.value
  clearScope.value = 'history'
  clearFailure.value = ''
  clearDialog.value = true
}
async function clearLogs() {
  if (clearing.value) return
  clearing.value = true
  clearFailure.value = ''
  try {
    const result = await props.api('/logs/clear', { method:'POST', body:JSON.stringify({tool:clearTarget.value, scope:clearScope.value}) })
    clearResult.value = `已清除 ${result.deleted} 个日志文件，释放 ${(result.freed_bytes / 1024 / 1024).toFixed(2)} MB；保留 ${result.kept} 个当日日志，跳过 ${result.skipped} 个运行中或受保护日志。`
    if (result.errors.length) clearResult.value += ` ${result.errors.length} 个文件清理失败：` + result.errors.slice(0, 3).map(item => `${item.source}：${item.reason}`).join('；')
    clearDialog.value = false
    emit('notice', `日志清理完成，已清除 ${result.deleted} 个文件。`)
    if (page.value !== 1) page.value = 1
    await load()
  } catch (error) { clearFailure.value = error.message }
  finally { clearing.value = false }
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
      <v-btn prepend-icon="mdi-refresh" variant="tonal" :loading="loading" @click="load">刷新日志</v-btn>
      <v-btn prepend-icon="mdi-delete-outline" variant="outlined" color="error" @click="openClear">清除日志</v-btn>
    </div>
    <v-row class="px-5" dense>
      <v-col cols="12" sm="3"><v-select v-model="tool" :items="[{title:'全部工具',value:''},{title:'FusionLive 同步',value:'sync'},{title:'绿联云上传',value:'nas'},{title:'微信与 Codex',value:'wechat'}]" label="工具" hide-details /></v-col>
      <v-col cols="12" sm="3"><v-select v-model="level" :items="levels" label="日志级别" hide-details /></v-col>
      <v-col cols="12" sm="6"><v-text-field v-model="search" label="搜索日志" placeholder="文件名、任务、错误…" prepend-inner-icon="mdi-magnify" clearable @click:clear="search = ''" hide-details /></v-col>
    </v-row>
    
    <v-alert v-if="clearResult" type="info" variant="tonal" class="mx-5 my-4" closable @click:close="clearResult = ''">{{ clearResult }}</v-alert>
    <v-alert v-if="failure" type="error" variant="tonal" class="mx-5 mb-4">{{ failure }}</v-alert>
    <v-progress-linear v-if="loading" indeterminate color="primary" />
    <div v-if="data.items.length" class="px-5 pb-4">
      <article v-for="item in data.items" :key="item.id" class="log-entry py-4">
        <div class="d-flex flex-wrap ga-2 align-center mb-2">
          <v-chip :color="colors[item.level] || 'secondary'" size="small" label>{{ item.level }}</v-chip>
          <time class="text-caption">{{ new Date(item.created_at).toLocaleString('zh-CN', { hour12: false, timeZone:'Asia/Shanghai' }) }}</time>
          <span class="text-caption text-secondary">{{ item.source }}</span>
        </div>
        <p class="log-text text-body-2">{{ item.message }}</p>
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
    <div class="px-5 pb-3 text-caption text-secondary">本次读取匹配 {{ data.total }} 条 · {{ data.scope || '最近日志尾部' }}。关闭“合并进度”可查看逐条记录；原始日志文件保留。</div>
    <v-pagination v-if="data.total > 20" v-model="page" :length="Math.ceil(data.total / 20)" :total-visible="4" page-aria-label="第 {0} 页" current-page-aria-label="当前第 {0} 页" aria-label="日志分页" />
    <v-dialog v-model="clearDialog" max-width="540" :persistent="clearing">
      <v-card title="清除日志">
        <v-card-text>
          <p class="mb-4">清理工具：{{ ({sync:'FusionLive 同步',nas:'绿联云上传',wechat:'微信与 Codex'})[clearTarget] || '全部工具及管理中心' }}</p>
          <v-select v-model="clearScope" label="清理范围" :disabled="clearing" :items="[{title:'仅清除今天之前的日志（默认）',value:'history'},{title:'清除全部日志，包含今天',value:'all'}]" />
          <p class="text-body-2 mt-3">按文件最后更新时间判断历史日志。仅删除日志文件，保留台账、调度状态、解析缓存、会话及发送记录。运行中工具的日志会跳过，可安全停止对应工具后再清理。</p>
          <p class="text-body-2 mt-3">清理范围不受搜索词、日志级别或分页影响。删除后无法恢复。</p>
          <v-alert v-if="clearFailure" type="error" variant="tonal" class="mt-4">{{ clearFailure }}</v-alert>
        </v-card-text>
        <v-card-actions><v-spacer /><v-btn :disabled="clearing" @click="clearDialog = false">取消</v-btn><v-btn color="error" variant="flat" :loading="clearing" @click="clearLogs">确认清除</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
  </v-card>
</template>

<style scoped>
.log-entry + .log-entry { border-top: 1px solid rgba(var(--v-theme-on-surface), .1); }
.log-text, .log-details { overflow-wrap: anywhere; white-space: pre-wrap; }
.log-details { background: rgba(var(--v-theme-on-surface), .04); border-radius: 8px; font-size: 12px; }
summary { cursor: pointer; }
</style>
