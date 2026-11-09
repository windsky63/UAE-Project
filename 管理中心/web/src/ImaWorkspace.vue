<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { VTabs, VTab } from 'vuetify/components'
import ImaPagination from './ImaPagination.vue'
import LogClearButton from './LogClearButton.vue'
const props = defineProps({ api: { type: Function, required: true } })
const emit = defineEmits(['notice'])
const snapshot = ref(null), form = ref({}), revision = ref(''), failure = ref(''), summaryError = ref(''), busy = ref(false)
const section = ref('work'), tab = ref('plan'), recordTab = ref('tasks')
const bases = ref([]), local = ref([]), cloud = ref([]), localLoaded = ref(false), cloudLoaded = ref(false)
const clientId = ref(''), apiKey = ref(''), query = ref(''), cloudFolder = ref(''), breadcrumbs = ref([]), cloudBase = ref('')
const dialog = ref(null), input = ref(''), destinations = ref([]), selectedDestination = ref('')
const page = ref(1), size = ref(20), searchText = ref(''), status = ref(''), loadingRows = ref(false), rowsError = ref('')
const serverRows = ref({ items: [], total: 0 }), submitted = ref(null), taskLog = ref(null), logRows = ref({ items: [], total: 0 }), logPage = ref(1), logBusy = ref(false), logError = ref('')
const names = { cloud_existing: '云端已存在', ready: '待上传', ready_update: '待更新', ready_delete: '待逻辑删除', unchanged: '未变化', done: '上传成功', deleted: '已逻辑删除', deleted_local: '本地已逻辑删除', conflict: '冲突', blocked: '已拦截', failed: '失败', uncertain: '待核对', superseded: '旧版已归档', uploading: '上传中', adding: '入库中', archiving: '归档中', archived: '已归档', copied: '本地提取成功', updated: '本地更新成功', error: '提取失败', duplicate: '重复记录', placeholder: '占位记录', would_copy: '将提取', would_update: '将更新' }
const taskNames = { start: '自动同步', stop: '安全停止', collect: '提取本地资料', plan: '预览上传计划', 'sync-once': '手动同步', check: '本地扫描' }
const stateNames = { running: '运行中', external: '外部运行中', stopped: '未启动', succeeded: '已完成', failed: '失败', detached: '待核对' }
const sectionItems = [{ value: 'work', title: '同步工作区' }, { value: 'cloud', title: '云端内容' }, { value: 'records', title: '任务与记录' }, { value: 'settings', title: '同步配置' }]
const active = computed(() => snapshot.value && snapshot.value.state !== 'stopped')
const dirty = computed(() => snapshot.value && Object.keys(snapshot.value.data).some(key => form.value[key] !== snapshot.value.data[key]))
const tasksRunning = computed(() => snapshot.value?.tasks.some(item => item.state === 'running' && item.action !== 'stop'))
const kind = computed(() => section.value === 'records' ? recordTab.value : section.value === 'cloud' ? 'cloud' : section.value === 'work' ? tab.value : '')
const clientSide = computed(() => ['local', 'cloud'].includes(kind.value))
const title = computed(() => ({ plan: '最近上传计划', local: '本地文件', collection: '资料提取结果', cloud: '云端内容', tasks: '后台任务', history: '上传记录', journal: '每日文件更新记录' }[kind.value] || ''))
const descriptions = { plan: '仅预览，不会上传文件。核对计划后再执行同步。', local: '扫描待同步目录，检查文件能否上传。', collection: '提取成功表示已复制到本地待同步目录，不代表已上传 ima。', cloud: '浏览所选知识库，进入文件夹后可设为同步目标。', tasks: '“已提交”只表示任务开始；以这里的最终状态和退出码为准。', history: '查看云端上传回执，失败或待核对的记录需处理后再重试。', journal: '本地提取与云端同步分别记录；“提取新增”成功不代表云端上传成功。' }
const targetName = computed(() => bases.value.find(item => item.id === snapshot.value?.data.kb)?.name || snapshot.value?.data.kb || '尚未选择知识库')
const targetFolder = computed(() => snapshot.value?.data.folder ? '子目录 ' + snapshot.value.data.folder : '根目录')
const latestPlanTask = computed(() => snapshot.value?.tasks.find(item => item.action === 'plan'))
const trackedTask = computed(() => submitted.value ? snapshot.value?.tasks.find(item => item.id === submitted.value.id) || submitted.value : null)
const currentTask = computed(() => trackedTask.value || latestPlanTask.value)
const collectionIssues = computed(() => (snapshot.value?.collection?.counts?.error || 0) + (snapshot.value?.collection?.counts?.conflict || 0))
const currentTaskCollectionWarning = computed(() => currentTask.value?.action === 'collect' && currentTask.value.state === 'succeeded' && collectionIssues.value > 0 && new Date(snapshot.value?.collection?.updated_at || 0) >= new Date(currentTask.value.started || 0))
const planOld = computed(() => ['failed', 'running', 'detached'].includes(latestPlanTask.value?.state))
const planDifferent = computed(() => snapshot.value?.plan && (snapshot.value.plan.knowledge_base_id !== snapshot.value.data.kb || snapshot.value.plan.folder_id !== snapshot.value.data.folder || snapshot.value.plan.source !== snapshot.value.data.source))
const countText = computed(() => Object.entries(snapshot.value?.plan?.counts || {}).map(([key, value]) => `${names[key] || key} ${value}`).join(' · '))
const filtered = computed(() => {
  const needle = String(searchText.value || '').trim().toLocaleLowerCase()
  return (kind.value === 'cloud' ? cloud.value : local.value).filter(item => (!status.value || item.status === status.value) && (!needle || [item.relative, item.title, item.name, item.reason].some(value => String(value || '').toLocaleLowerCase().includes(needle))))
})
const total = computed(() => clientSide.value ? filtered.value.length : serverRows.value.total)
const rows = computed(() => clientSide.value ? filtered.value.slice((page.value - 1) * size.value, page.value * size.value) : serverRows.value.items)
const choices = computed(() => {
  const values = kind.value === 'tasks' ? ['running', 'succeeded', 'failed', 'detached'] : kind.value === 'journal' ? ['成功', '失败', '冲突', '待核对'] : kind.value === 'collection' ? ['copied', 'updated', 'unchanged', 'error', 'conflict', 'duplicate', 'placeholder', 'would_copy', 'would_update'] : ['ready', 'ready_update', 'ready_delete', 'unchanged', 'done', 'failed', 'conflict', 'blocked', 'uncertain', 'deleted', 'archived', 'uploading', 'adding', 'retry']
  return [{ title: '全部状态', value: '' }, ...values.map(value => ({ title: (kind.value === 'tasks' ? stateNames[value] : names[value]) || value, value }))]
})
const isFolder = item => item.media_type === 99 || item.folder_id || item.folder_info?.folder_id
const folderId = item => item.folder_info?.folder_id || item.folder_id || item.media_id
let timer, debounce, closed = false, rowVersion = 0, summaryVersion = 0, logVersion = 0
function notice(text, error = false) { emit('notice', text, error) }
function stamp(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '—' }
function tone(value) { return ['failed', 'error', 'conflict', '失败', '冲突'].includes(value) ? 'bad' : ['succeeded', 'done', 'copied', 'updated', '成功'].includes(value) ? 'good' : ['ready_update', 'uncertain', 'detached', '待核对'].includes(value) ? 'warn' : ['ready', 'running', 'ready_delete'].includes(value) ? 'blue' : 'muted' }
async function request(operation, data = {}) { return props.api('/ima/query', { method: 'POST', body: JSON.stringify({ operation, data }) }) }
async function loadRows() {
  const current = ++rowVersion
  clearTimeout(debounce)
  if (!kind.value || clientSide.value) { loadingRows.value = false; return }
  loadingRows.value = true; rowsError.value = ''
  try {
    const params = new URLSearchParams({ kind: kind.value, offset: String((page.value - 1) * size.value), limit: String(size.value), search: String(searchText.value || '').trim(), status: status.value })
    const value = await props.api('/ima/records?' + params)
    if (closed || current !== rowVersion) return
    serverRows.value = value
    const actual = Math.floor(value.offset / size.value) + 1
    if (actual !== page.value) page.value = actual
  } catch (error) { if (current === rowVersion) rowsError.value = error.message }
  finally { if (current === rowVersion) loadingRows.value = false }
}
async function load(reset = false) {
  const current = ++summaryVersion
  try {
    const value = await props.api('/ima')
    if (closed || current !== summaryVersion) return
    const previous = snapshot.value
    snapshot.value = value; summaryError.value = ''
    if (reset) { form.value = { ...value.data }; revision.value = value.revision }
    if (previous && ((kind.value === 'plan' && previous.plan?.revision !== value.plan?.revision) || (kind.value === 'collection' && previous.collection?.revision !== value.collection?.revision) || (section.value === 'records' && JSON.stringify(previous.tasks) !== JSON.stringify(value.tasks)))) await loadRows()
  } catch (error) { if (current === summaryVersion) summaryError.value = error.message }
}
async function execute(work) {
  if (busy.value) return
  busy.value = true; failure.value = ''
  try { return await work() } catch (error) { failure.value = error.message; notice(error.message, true) }
  finally { busy.value = false }
}
async function save() { await execute(async () => {
  const value = await props.api('/config', { method: 'POST', body: JSON.stringify({ tool: 'ima', data: form.value, revision: revision.value }) })
  revision.value = value.revision; form.value = { ...value.data }; await load()
  notice('ima 配置已保存。同步任务使用已保存的配置；运行中需安全停止后重启。')
}) }
async function credentials() { await execute(async () => {
  try { await request('credentials', { client_id: clientId.value, api_key: apiKey.value }); notice('凭证已在服务器加密保存。'); await load() }
  finally { clientId.value = ''; apiKey.value = '' }
}) }
async function readBases() { await execute(async () => { bases.value = (await request('bases')).items; notice(`已读取 ${bases.value.length} 个可写知识库。`) }) }
async function scan() { await execute(async () => { local.value = (await request('scan')).items; localLoaded.value = true; section.value = 'work'; tab.value = 'local'; resetFilters(); notice('本地扫描完成。') }) }
async function browse(folder = cloudFolder.value, resetTrail = false) { await execute(async () => {
  cloud.value = (await request('list', { kb: form.value.kb, folder })).items
  cloudFolder.value = folder; cloudBase.value = form.value.kb; cloudLoaded.value = true
  if (resetTrail) breadcrumbs.value = []
  section.value = 'cloud'; resetFilters()
}) }
async function open(item) {
  if (!isFolder(item)) return
  const before = cloudFolder.value
  await browse(folderId(item))
  if (!failure.value) breadcrumbs.value.push({ id: before, title: item.title || item.name || item.folder_info?.name })
}
async function up() { const before = breadcrumbs.value.at(-1); await browse(before?.id || ''); if (!failure.value) breadcrumbs.value.pop() }
async function searchCloud() { await execute(async () => { cloud.value = (await request('search', { kb: form.value.kb, query: query.value })).items; cloudBase.value = form.value.kb; cloudLoaded.value = true; resetFilters() }) }
function setTarget() { if (!cloudLoaded.value || cloudBase.value !== form.value.kb) return; form.value.folder = cloudFolder.value; section.value = 'settings'; notice('已选择目标目录，请点击“保存 ima 配置”使其生效。') }
function confirmTask(action) { dialog.value = { kind: 'task', action, title: taskNames[action], text: action === 'collect' ? '按下载数据库提取资料到本地待同步目录，原文件保留。这一步不会上传到 ima。' : action === 'start' ? '按已保存的北京时间每日定时同步，一天最多一次（含手动）；本地新版直接覆盖，云端旧版移入删除目录。' : '立即执行今天的一次同步；当天已同步则不再执行。本地新版直接覆盖，云端旧版移入删除目录。' } }
async function task(action) { await execute(async () => {
  const value = await props.api('/action', { method: 'POST', body: JSON.stringify({ tool: 'ima', action }) })
  submitted.value = { ...value, action, label: taskNames[action] }
  if (action === 'plan' || action === 'collect') { section.value = 'work'; tab.value = action === 'plan' ? 'plan' : 'collection'; resetFilters() }
  notice(`${taskNames[action]}已提交，正在后台处理；尚未完成。`); await load(); await loadRows()
}) }
function deleteLocal(item) { dialog.value = { kind: 'query', operation: 'move-local', data: { path: item.path }, title: '移入本地删除目录', text: `将 ${item.relative} 移入待同步下的“删除”目录；保留文件，下次同步处理对应云端文件。` } }
function resolve(item, uploaded) { dialog.value = { kind: 'query', operation: 'resolve', data: { path: item.path, folder: item.target_folder_id ?? snapshot.value.data.folder, uploaded }, title: '记录人工核对结果', text: uploaded ? '确认已在 ima 中找到本次上传的文件？' : '确认本次上传没有入库，允许再次上传？' } }
function rename(item) { input.value = ''; dialog.value = { kind: 'rename', item, title: '重命名文件', text: '仅允许操作本地有同名文件及上传回执的条目；新名称也必须在本地存在。' } }
function createFolder() { input.value = ''; dialog.value = { kind: 'folder', title: '创建同名云端文件夹', text: '只创建本地同步目录中已经存在的对应文件夹。' } }
async function move(item) { await execute(async () => { destinations.value = (await request('destinations', { kb: cloudBase.value })).items; selectedDestination.value = ''; dialog.value = { kind: 'move', item, title: '移动文件', text: '目标列表仅包含本地同步目录中对应的云端目录。' } }) }
async function confirmed() {
  const action = dialog.value
  if (!action) return
  if (action.kind === 'task') { dialog.value = null; return task(action.action) }
  await execute(async () => {
    const item = action.item || {}
    const common = { kb: cloudBase.value, folder: item.parent_folder_id || cloudFolder.value, media_id: item.media_id, title: item.title }
    if (action.kind === 'query') await request(action.operation, action.data)
    else if (action.kind === 'rename') await request('rename', { ...common, name: input.value })
    else if (action.kind === 'folder') await request('create-folder', { kb: cloudBase.value, folder: cloudFolder.value, name: input.value })
    else if (action.kind === 'move') await request('move', { ...common, destination: selectedDestination.value })
    dialog.value = null; notice('操作完成。'); await load(); await loadRows()
    if (action.kind === 'query' && action.operation === 'move-local') local.value = (await request('scan')).items
    else if (action.kind !== 'query') cloud.value = (await request('list', { kb: cloudBase.value, folder: cloudFolder.value })).items
  })
}
async function logsCleared() {
  if (taskLog.value) {
    if (logPage.value !== 1) logPage.value = 1
    else await loadLog()
  }
}
async function viewLog(item) { taskLog.value = item; logPage.value = 1; await loadLog() }
async function loadLog() {
  if (!taskLog.value) return
  const current = ++logVersion
  logBusy.value = true; logError.value = ''
  try { const value = await props.api('/logs?' + new URLSearchParams({ tool: 'ima', search: taskLog.value.log || '', compact: '0', offset: String((logPage.value - 1) * 20), limit: '20' })); if (current === logVersion && !closed) logRows.value = value }
  catch (error) { if (current === logVersion) logError.value = error.message }
  finally { if (current === logVersion) logBusy.value = false }
}
function resetFilters() { rowVersion++; searchText.value = ''; status.value = ''; page.value = 1; serverRows.value = { items: [], total: 0 }; clearTimeout(debounce); loadRows() }
function filterChanged() { rowVersion++; clearTimeout(debounce); debounce = setTimeout(() => { if (page.value !== 1) page.value = 1; else loadRows() }, 250) }
function changeSize(value) { size.value = value; if (page.value !== 1) page.value = 1; else loadRows() }
function showRecords(value = 'tasks') { section.value = 'records'; recordTab.value = value }
watch(kind, resetFilters); watch(page, loadRows); watch(searchText, filterChanged); watch(status, filterChanged); watch(logPage, loadLog)
watch(() => form.value.kb, (value, old) => { if (old && value !== old) { if (value !== snapshot.value?.data.kb || form.value.folder !== snapshot.value?.data.folder) form.value.folder = ''; cloud.value = []; cloudLoaded.value = false; cloudFolder.value = ''; breadcrumbs.value = [] } })
watch(total, value => { const last = Math.max(1, Math.ceil(value / size.value)); if (page.value > last) page.value = last })
onMounted(async () => { await load(true); if (closed) return; await loadRows(); if (closed) return; timer = setInterval(() => { if (!document.hidden && !busy.value) load() }, 3000) })
onUnmounted(() => { closed = true; rowVersion++; summaryVersion++; logVersion++; clearInterval(timer); clearTimeout(debounce) })
</script>

<template>
  <div class="ima-panel">
    <v-alert v-if="summaryError || failure" type="error" variant="tonal" class="mb-4">{{ failure || summaryError }}</v-alert>
    <template v-if="snapshot">
      <div class="ima-target"><div class="ima-target-main"><span>已保存同步目标</span><strong>{{ targetName }}</strong><span class="ima-target-divider">/</span><span class="ima-path">{{ targetFolder }}</span></div><div class="ima-target-state"><span>自动同步</span><span :class="['ima-state', tone(snapshot.state)]">{{ stateNames[snapshot.state] || snapshot.state }}</span></div><v-btn variant="outlined" size="small" @click="section = 'settings'">配置</v-btn></div>
      <nav class="ima-sections" aria-label="ima 功能分区"><button v-for="item in sectionItems" :key="item.value" :aria-label="item.title" :class="{ selected: section === item.value }" :aria-current="section === item.value ? 'page' : undefined" @click="section = item.value">{{ item.title }}<span v-if="item.value === 'settings' && dirty" class="ima-unsaved-dot" aria-label="有未保存配置"/></button></nav>
      <v-alert v-if="dirty && section !== 'settings'" type="warning" variant="tonal" class="mb-4">配置尚未保存。预览和同步仍使用上方已保存的目标。<v-btn variant="text" size="small" @click="section = 'settings'">前往保存</v-btn></v-alert>
      <template v-if="section === 'work'">
        <div class="ima-workflow"><div class="ima-step"><span class="ima-step-number">1</span><div><h2>提取本地资料</h2><p>下载目录 → 待同步</p><v-btn variant="outlined" size="small" :disabled="busy || active || tasksRunning || dirty" @click="confirmTask('collect')">提取本地资料</v-btn></div></div><div class="ima-step"><span class="ima-step-number">2</span><div><h2>预览上传计划</h2><p>核对计划，不上传文件</p><v-btn color="primary" size="small" :disabled="busy || tasksRunning || dirty" @click="task('plan')">预览上传计划</v-btn></div></div><div class="ima-step"><span class="ima-step-number">3</span><div><h2>执行同步</h2><p>上传新版，归档旧版</p><v-btn variant="outlined" size="small" :disabled="busy || active || tasksRunning || dirty" @click="confirmTask('sync-once')">手动同步</v-btn></div></div></div>
        <div v-if="currentTask" :class="['ima-task-banner', currentTaskCollectionWarning ? 'warn' : tone(currentTask.state)]" role="status" aria-live="polite"><div><strong>{{ taskNames[currentTask.action] || currentTask.label }} · {{ currentTaskCollectionWarning ? '已完成，有提取问题' : stateNames[currentTask.state] || currentTask.state }}</strong><p>{{ currentTaskCollectionWarning ? '提取已完成，部分记录缺失或冲突；其余可用文件可继续预览和同步。' : currentTask.state === 'running' ? '任务已提交，后台正在处理。请等待最终结果。' : currentTask.state === 'failed' ? '本次任务失败。请查看任务日志；已有结果可能来自上一次运行。' : currentTask.state === 'succeeded' ? '任务已完成，请在下方查看对应结果。' : '请在任务记录中核对最终结果。' }}</p></div><v-btn v-if="currentTaskCollectionWarning" variant="text" size="small" @click="tab = 'collection'">查看提取问题</v-btn><v-btn v-if="currentTask.log" variant="text" size="small" @click="viewLog(currentTask)">查看任务日志</v-btn><v-btn variant="text" size="small" @click="showRecords()">所有任务</v-btn></div>
        <v-alert v-if="collectionIssues && !currentTaskCollectionWarning" type="warning" variant="tonal" class="mb-4">最近提取有 {{ collectionIssues }} 项问题，已跳过相应记录；其余可用文件可继续预览和同步。<v-btn variant="text" size="small" @click="tab = 'collection'">查看提取结果</v-btn></v-alert>
      </template>
      <v-card v-if="section === 'settings'" class="ima-settings">
        <div class="ima-panel-heading"><div><h2>同步配置</h2><p>先配置并保存，再回到同步工作区生成计划。</p></div><v-btn variant="text" :disabled="busy" @click="load(true)">重新读取配置</v-btn></div>
        <div class="ima-settings-body">
          <v-alert type="info" variant="tonal" class="mb-4">直接只读同步工具数据库，无需导出 Excel。默认数据库为 FusionLive同步工具/runtime/ledger.sqlite3。</v-alert>
          <section><h3>目标知识库</h3><p>同步任务使用已保存的目标。读取目录后，可将当前目录设为同步目标。</p><div class="ima-target-fields"><v-select v-model="form.kb" :items="bases.length ? bases : [{ id: form.kb, name: form.kb ? '已保存的知识库：' + form.kb : '尚未选择' }]" item-title="name" item-value="id" label="目标知识库"/><v-btn variant="outlined" :disabled="busy" @click="readBases">读取知识库</v-btn><v-btn variant="text" :disabled="!form.kb || busy" @click="browse('', true)">浏览目标目录</v-btn></div><p>{{ form.folder ? '子目录为同步目标：' + form.folder : '根目录为同步目标' }}</p></section>
          <section><h3>本地资料位置</h3><div class="ima-fields"><v-text-field v-model="form.source" label="待同步目录"/><v-text-field v-model="form.database_path" label="下载数据库路径"/><v-text-field v-model="form.downloads_path" label="下载原目录"/></div><div class="ima-checks"><v-checkbox v-model="form.collect_enabled" label="同步前提取数据库资料" hide-details/><v-checkbox v-model="form.recursive" label="包含子目录" hide-details/></div><p>固定沿用原脚本范围：全部专业 PP 正文，不含关联附件和指南培训。复制到“项目管理规定 / 01～07 分类 / 文件”。</p></section>
          <section><h3>自动同步</h3><div class="ima-auto-settings"><v-switch v-model="form.auto_sync_enabled" label="允许自动同步"/><v-text-field v-model="form.sync_time" label="每日同步时间（北京时间）" type="time" :disabled="!form.auto_sync_enabled"/></div><p>一天最多同步一次（含手动），失败后次日再试。关闭并保存后将安全停止；开启并保存后，需点击“启动自动同步”。</p></section>
          <div class="ima-save-bar"><span :class="{ 'ima-dirty-text': dirty }">{{ dirty ? '配置有未保存的修改' : '配置已保存' }}</span><v-btn color="primary" :loading="busy" @click="save">保存 ima 配置</v-btn></div>
        </div>
        <v-expansion-panels class="ima-credentials"><v-expansion-panel :title="snapshot.credentials_configured ? 'ima 凭证：已配置（点击更换）' : 'ima 凭证：尚未配置'"><v-expansion-panel-text><div class="ima-fields"><v-text-field v-model="clientId" label="Client ID" autocomplete="off"/><v-text-field v-model="apiKey" label="API Key" type="password" autocomplete="new-password"/></div><div class="ima-actions"><v-btn color="primary" :disabled="busy || !clientId || !apiKey" @click="credentials">加密保存凭证</v-btn><v-btn href="https://ima.qq.com/agent-interface" target="_blank" rel="noopener" variant="text">获取凭证</v-btn></div><p>凭证绑定服务器当前 Windows 用户；配置成功不代表接口验证通过。</p></v-expansion-panel-text></v-expansion-panel></v-expansion-panels>
      </v-card>
      <v-card v-else class="ima-results" :data-kind="kind">
        <v-tabs v-if="section === 'work'" v-model="tab" class="ima-result-tabs" aria-label="同步结果"><v-tab value="plan">上传计划</v-tab><v-tab value="local">本地文件</v-tab><v-tab value="collection">提取结果</v-tab></v-tabs>
        <v-tabs v-if="section === 'records'" v-model="recordTab" class="ima-result-tabs" aria-label="记录类型"><v-tab value="tasks">后台任务</v-tab><v-tab value="history">上传记录</v-tab><v-tab value="journal">每日记录</v-tab></v-tabs>
        <div class="ima-panel-heading"><div><h2>{{ title }}</h2><p>{{ descriptions[kind] }}</p></div><v-btn v-if="kind === 'local'" variant="outlined" :disabled="busy" @click="scan">扫描本地文件</v-btn><v-btn v-else-if="kind !== 'cloud'" variant="text" prepend-icon="mdi-refresh" :loading="loadingRows" @click="load(); loadRows()">刷新</v-btn></div>
        <template v-if="kind === 'plan'"><v-alert v-if="planOld" :type="latestPlanTask.state === 'failed' ? 'error' : 'info'" variant="tonal" class="ima-inline-alert">{{ latestPlanTask.state === 'failed' ? '最近一次预览失败，本次未生成成功的计划。' : '最近一次预览尚未确认完成。' }}{{ snapshot.plan ? ' 下方保留上次可用结果，请核对生成时间。' : '' }}</v-alert><v-alert v-if="planDifferent" type="warning" variant="tonal" class="ima-inline-alert">这份计划与当前已保存的配置不同，请重新预览。</v-alert><v-alert v-if="snapshot.plan" type="info" variant="tonal" class="ima-inline-alert">{{ snapshot.plan.layout === 'relative-tree-v1' ? '按本地相对目录逐层匹配云端；云端已存在的手工文件跳过上传，内容尚未核验。缺少的目录仅在正式同步时创建。' : '这是旧版平铺计划，请重新点击预览上传计划。' }}</v-alert><div v-if="snapshot.plan" class="ima-result-meta"><span>生成于 {{ stamp(snapshot.plan.updated_at) }}</span><span>{{ snapshot.plan.executed ? '同步执行结果' : '只读预览' }}</span><span>{{ countText }}</span></div></template>
        <div v-if="kind === 'collection' && snapshot.collection" class="ima-result-meta"><span>生成于 {{ stamp(snapshot.collection.updated_at) }}</span><span>本地提取 · {{ snapshot.collection.dry_run ? '预览结果，未复制文件' : '已执行复制' }}</span></div>
        <div v-if="kind === 'journal'" class="ima-result-meta ima-path">{{ snapshot.journal_path }}</div>
        <template v-if="kind === 'cloud'"><div class="ima-cloud-target"><v-select v-model="form.kb" :items="bases.length ? bases : [{ id: form.kb, name: form.kb || '尚未选择' }]" item-title="name" item-value="id" label="浏览知识库"/><v-btn variant="outlined" :disabled="busy" @click="readBases">读取知识库</v-btn></div><div class="ima-cloud-toolbar"><v-btn variant="text" :disabled="busy || !form.kb" @click="browse('', true)">根目录</v-btn><v-btn variant="text" :disabled="busy || !cloudFolder" @click="up">上一级</v-btn><v-btn variant="outlined" :disabled="busy || !form.kb" @click="browse()">读取当前目录</v-btn><v-btn variant="outlined" :disabled="busy || !cloudLoaded || cloudBase !== form.kb" @click="setTarget">设为同步目标</v-btn><v-menu><template #activator="{ props: menuProps }"><v-btn v-bind="menuProps" variant="text" :disabled="busy || !cloudLoaded || cloudBase !== form.kb">目录操作</v-btn></template><v-list><v-list-item title="创建同名云端文件夹" @click="createFolder"/></v-list></v-menu></div><div class="ima-result-meta"><span>当前位置：{{ breadcrumbs.map(item => item.title).join(' / ') || '根目录' }}</span><span v-if="dirty">浏览选择尚未保存为同步配置</span></div><div class="ima-cloud-search"><v-text-field v-model="query" label="知识库搜索" placeholder="输入关键词后查询云端" @keyup.enter="searchCloud"/><v-btn variant="outlined" :disabled="busy || !form.kb || !query.trim()" @click="searchCloud">搜索知识库</v-btn></div></template>
        <div class="ima-filters"><v-text-field v-model="searchText" :label="kind === 'cloud' ? '筛选已读取的文件' : '搜索文件或记录'" prepend-inner-icon="mdi-magnify" clearable @click:clear="searchText = ''"/><v-select v-if="kind !== 'cloud'" v-model="status" :items="choices" label="状态筛选"/></div>
        <v-progress-linear v-if="loadingRows" indeterminate color="primary" aria-label="正在读取记录"/>
        <v-alert v-if="rowsError" type="error" variant="tonal" class="ima-inline-alert">{{ rowsError }}</v-alert>
        <v-table v-else-if="rows.length" class="ima-table" :aria-label="title">
          <thead><tr v-if="kind === 'tasks'"><th>操作</th><th>状态</th><th>开始时间</th><th>退出码</th><th>日志</th></tr><tr v-else-if="kind === 'journal'"><th>时间（北京时间）</th><th>操作</th><th>文件</th><th>结果</th><th>详情</th></tr><tr v-else-if="kind === 'cloud'"><th>名称</th><th>类型</th><th>操作</th></tr><tr v-else-if="kind === 'history'"><th>文件</th><th>结果</th><th>时间（北京时间）</th><th>说明</th></tr><tr v-else><th>文件</th><th v-if="kind === 'plan'">云端目标路径（相对所选目录）</th><th>状态</th><th>说明</th><th v-if="kind !== 'collection'">操作</th></tr></thead>
          <tbody><tr v-for="(item, index) in rows" :key="item.id || item.media_id || index">
            <template v-if="kind === 'tasks'"><td>{{ item.label }}</td><td><span :class="['ima-state', tone(item.state)]">{{ stateNames[item.state] || item.state }}</span></td><td class="ima-time">{{ stamp(item.started) }}</td><td>{{ item.exit_code ?? '—' }}</td><td><v-btn variant="text" size="small" :disabled="!item.log" @click="viewLog(item)">查看日志</v-btn></td></template>
            <template v-else-if="kind === 'journal'"><td class="ima-time">{{ item.time }}</td><td>{{ item.operation }}<small v-if="item.operation.startsWith('提取')" class="ima-local-label">本地资料</small></td><td class="ima-path">{{ item.path }}</td><td><span :class="['ima-state', tone(item.status)]">{{ item.status }}</span></td><td class="ima-path">{{ item.reason }}</td></template>
            <template v-else-if="kind === 'cloud'"><td><button v-if="isFolder(item)" class="ima-folder-link" @click="open(item)"><v-icon icon="mdi-folder-outline" size="18"/>{{ item.title || item.name || item.folder_info?.name }}</button><span v-else class="ima-path">{{ item.title || item.name }}</span></td><td>{{ isFolder(item) ? '文件夹' : '文件' }}</td><td><v-btn v-if="isFolder(item)" variant="text" size="small" :disabled="busy" @click="open(item)">进入</v-btn><v-menu v-else><template #activator="{ props: menuProps }"><v-btn v-bind="menuProps" variant="text" size="small" :disabled="busy">文件操作</v-btn></template><v-list><v-list-item title="重命名" @click="rename(item)"/><v-list-item title="移动文件" @click="move(item)"/></v-list></v-menu></td></template>
            <template v-else-if="kind === 'history'"><td class="ima-path">{{ item.path }}</td><td><span :class="['ima-state', tone(item.status)]">{{ names[item.status] || item.status }}</span></td><td class="ima-time">{{ stamp(item.updated * 1000) }}</td><td class="ima-path">{{ item.note || '—' }}</td></template>
            <template v-else><td class="ima-path">{{ item.relative || item.name }}<small v-if="kind === 'collection' && item.record_source === 'database'" class="ima-local-label">数据库 · 工作区 {{ item.project }} · 内容ID {{ item.file_id }}</small><small v-if="kind === 'collection' && item.record_source !== 'database' && item.ledger_row" class="ima-local-label">台账第 {{ item.ledger_row }} 行</small></td><td v-if="kind === 'plan'" class="ima-path">{{ item.target_path || '旧计划，请重新预览' }}</td><td><span :class="['ima-state', tone(item.status)]">{{ names[item.status] || item.status }}</span></td><td class="ima-path">{{ item.reason || (item.status === 'copied' ? '已复制到本地待同步目录' : item.status === 'ready' ? '本地文件可上传' : '—') }}</td><td v-if="kind !== 'collection'"><v-btn v-if="kind === 'local' && !item.action" variant="text" size="small" :disabled="busy || active" @click="deleteLocal(item)">移入删除目录</v-btn><div v-if="item.status === 'uncertain'" class="ima-actions"><v-btn variant="text" size="small" :disabled="busy" @click="resolve(item, true)">已入库</v-btn><v-btn variant="text" size="small" :disabled="busy" @click="resolve(item, false)">未入库</v-btn></div><span v-if="kind === 'plan' && item.status !== 'uncertain'" class="ima-muted">—</span></td></template>
          </tr></tbody>
        </v-table>
        <div v-else-if="!loadingRows" class="ima-empty"><v-icon icon="mdi-file-document-outline" size="30"/><strong>{{ searchText || status ? '没有符合条件的记录' : kind === 'plan' ? '还没有可用的上传计划' : kind === 'local' && !localLoaded ? '先扫描本地待同步文件' : kind === 'cloud' && !cloudLoaded ? '选择知识库并读取目录' : '暂无记录' }}</strong><p>{{ kind === 'plan' ? '点击上方“预览上传计划”，等待任务完成后在这里查看。' : searchText || status ? '调整关键词或状态筛选后重试。' : descriptions[kind] }}</p></div>
        <ImaPagination :total="total" :page="page" :size="size" :loading="loadingRows" :label="title" @page="page = $event" @size="changeSize"/>
      </v-card>
      <div v-if="section === 'work'" class="ima-auto-bar"><div><strong>自动同步</strong><span :class="['ima-state', tone(snapshot.state)]">{{ stateNames[snapshot.state] || snapshot.state }}</span><p>{{ snapshot.data.auto_sync_enabled ? `北京时间每天 ${snapshot.data.sync_time || '13:00'}，一天最多一次（含手动）` : '当前仅允许手动同步，可在同步配置中开启' }}</p></div><div class="ima-actions"><v-btn color="primary" variant="outlined" :disabled="busy || active || tasksRunning || dirty || !snapshot.data.auto_sync_enabled" @click="confirmTask('start')">启动自动同步</v-btn><v-btn variant="outlined" :disabled="busy || !active" @click="task('stop')">安全停止</v-btn></div></div>
    </template>
    <div v-else class="ima-empty"><v-progress-circular v-if="!summaryError" indeterminate color="primary"/><strong>{{ summaryError ? '无法读取 ima 状态' : '正在读取 ima 状态' }}</strong><v-btn v-if="summaryError" variant="text" @click="load(true)">重试</v-btn></div>
    <v-dialog :model-value="!!dialog" max-width="600" :persistent="busy" @update:model-value="value => { if (!value && !busy) dialog = null }"><v-card v-if="dialog" :title="dialog.title"><v-card-text><p>{{ dialog.text }}</p><v-text-field v-if="['folder', 'rename'].includes(dialog.kind)" v-model="input" label="名称" class="mt-4"/><v-select v-if="dialog.kind === 'move'" v-model="selectedDestination" :items="destinations" item-title="name" item-value="id" label="目标目录" class="mt-4"/><v-alert v-if="failure" type="error" class="mt-3">{{ failure }}</v-alert></v-card-text><v-card-actions><v-spacer/><v-btn :disabled="busy" @click="dialog = null">取消</v-btn><v-btn color="primary" :loading="busy" @click="confirmed">确认</v-btn></v-card-actions></v-card></v-dialog>
    <div v-if="section === 'records'" class="d-flex justify-end mb-4"><LogClearButton :api="api" tool="ima" label="清除ima日志" @notice="message => emit('notice', message)" @cleared="logsCleared" /></div>
    <v-dialog :model-value="!!taskLog" max-width="1000" @update:model-value="value => { if (!value) { taskLog = null; logVersion++ } }"><v-card v-if="taskLog"><div class="ima-panel-heading"><div><h2>{{ taskLog.label }} · 任务日志</h2><p class="ima-path">{{ taskLog.log }} · 退出码 {{ taskLog.exit_code ?? '尚未退出' }}</p></div><div class="d-flex flex-wrap ga-2"><v-btn variant="text" :loading="logBusy" @click="loadLog">刷新日志</v-btn><LogClearButton :api="api" tool="ima" @notice="message => emit('notice', message)" @cleared="logsCleared" /></div></div><v-alert v-if="logError" type="error" variant="tonal" class="ma-4">{{ logError }}</v-alert><div class="ima-log-lines"><p v-for="item in logRows.items" :key="item.id" :class="{ 'ima-log-error': item.level === 'ERROR' }">{{ item.message }}</p><p v-if="!logRows.items.length && !logBusy">暂无日志输出。</p></div><ImaPagination :total="logRows.total" :page="logPage" :size="20" fixed-size :loading="logBusy" label="任务日志" @page="logPage = $event"/><v-card-actions><v-spacer/><v-btn @click="taskLog = null; logVersion++">关闭</v-btn></v-card-actions></v-card></v-dialog>
  </div>
</template>

<style scoped>
.ima-task-banner.warn{background:#fff7e8;border-color:#c88720}
.ima-panel{min-width:0;color:#172b4d;font-size:14px}.ima-panel h2{font-size:16px;font-weight:650;line-height:1.5}.ima-panel p{line-height:1.7}.ima-target{display:flex;align-items:center;gap:22px;background:#fff;border:1px solid #dce4ef;border-radius:10px;padding:17px 22px}.ima-target-main{display:flex;align-items:center;gap:12px;flex:1;min-width:0;flex-wrap:wrap}.ima-target-main>span:first-child,.ima-target-state>span:first-child{color:#7589a7;font-size:12px}.ima-target-main strong{font-size:14px;overflow-wrap:anywhere}.ima-target-divider{color:#b8c5d7}.ima-target-state{display:flex;align-items:center;gap:12px;border-left:1px solid #e4eaf4;padding-left:24px;white-space:nowrap}.ima-sections{display:flex;gap:30px;border-bottom:1px solid #dce4ef;margin:17px 0 24px;overflow-x:auto}.ima-sections button{position:relative;background:none;border:0;border-bottom:2px solid transparent;padding:13px 3px;color:#667c9b;font-size:14px;font-family:inherit;white-space:nowrap;cursor:pointer}.ima-sections button.selected{border-color:#245bd6;color:#245bd6;font-weight:650}.ima-sections button:focus-visible,.ima-folder-link:focus-visible{outline:2px solid #245bd6;outline-offset:-2px}.ima-unsaved-dot{display:inline-block;background:#c88720;width:6px;height:6px;border-radius:50%;margin-left:6px;vertical-align:middle}.ima-workflow{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;margin-bottom:22px}.ima-step{display:flex;gap:14px;align-items:flex-start;padding:22px;background:#fff;border:1px solid #dce4ef;border-radius:10px}.ima-step-number{flex-shrink:0;display:flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:50%;color:#245bd6;background:#eef3ff;font-size:15px;font-weight:650}.ima-step p{font-size:12px;color:#7589a7;margin:4px 0 14px}.ima-panel-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:22px}.ima-panel-heading p{font-size:12px;color:#7589a7;margin-top:5px}.ima-result-tabs{border-bottom:1px solid #e5ebf4;padding:0 8px}.ima-result-tabs :deep(.v-tab){font-size:14px}.ima-filters{display:flex;gap:14px;padding:0 22px 20px}.ima-filters>.v-input:first-child{flex:1}.ima-filters>.v-select{flex:0 0 190px}.ima-filters :deep(.v-field){font-size:13px}.ima-table :deep(table){table-layout:fixed;min-width:660px;width:100%}.ima-table :deep(th){background:#f3f6fb!important;font-size:12px!important;font-weight:600!important;height:42px!important;color:#536b8e;padding:0 20px!important}.ima-table :deep(td){font-size:13px!important;line-height:1.6;padding:13px 20px!important;overflow-wrap:anywhere;white-space:normal!important;vertical-align:middle}.ima-table :deep(th:first-child){width:41%}.ima-table :deep(th:nth-child(2)){width:130px}.ima-results[data-kind=tasks] :deep(th:first-child){width:25%}.ima-results[data-kind=tasks] :deep(th:nth-child(3)){width:25%}.ima-results[data-kind=journal] :deep(table){min-width:900px}.ima-results[data-kind=journal] :deep(th:first-child){width:175px}.ima-results[data-kind=journal] :deep(th:nth-child(3)){width:33%}.ima-results[data-kind=history] :deep(table){min-width:820px}.ima-results[data-kind=history] :deep(th:nth-child(3)){width:190px}.ima-results[data-kind=cloud] :deep(th:first-child){width:62%}.ima-state{display:inline-block;border-radius:5px;padding:3px 8px;font-size:11px;line-height:1.5;white-space:nowrap;font-weight:500;background:#eef1f6;color:#667c9b}.ima-state.blue{color:#245bd6;background:#edf3ff}.ima-state.good{color:#16765c;background:#eaf6f0}.ima-state.warn{color:#a76513;background:#fff3df}.ima-state.bad{color:#bf3838;background:#fff0ee}.ima-task-banner{display:flex;align-items:center;gap:10px;padding:15px 20px;background:#edf3ff;border-left:3px solid #245bd6;border-radius:6px;margin:0 0 20px}.ima-task-banner>div{flex:1;min-width:0}.ima-task-banner strong{font-size:13px}.ima-task-banner p{font-size:12px;color:#667c9b;margin-top:4px}.ima-task-banner.bad{background:#fff4f2;border-color:#dc4545}.ima-task-banner.good{background:#edf8f3;border-color:#16836a}.ima-result-meta{display:flex;flex-wrap:wrap;gap:8px 20px;padding:0 22px 17px;font-size:12px;line-height:1.8;color:#7589a7}.ima-inline-alert{margin:0 22px 18px;font-size:13px}.ima-empty{display:flex;flex-direction:column;align-items:center;gap:12px;text-align:center;padding:50px 25px;color:#8a9bb4;min-height:210px}.ima-empty strong{font-weight:500;color:#536b8e;font-size:14px}.ima-empty p{max-width:500px;font-size:12px}.ima-path{overflow-wrap:anywhere;word-break:break-word;min-width:0}.ima-time{font-size:12px!important}.ima-local-label{display:block;font-size:11px;color:#8a9bb4;margin-top:4px}.ima-auto-bar{display:flex;justify-content:space-between;align-items:center;gap:16px;background:#fff;padding:21px 22px;border:1px solid #dce4ef;border-radius:10px;margin-top:22px}.ima-auto-bar strong{font-size:14px;margin-right:12px}.ima-auto-bar p{font-size:12px;color:#7589a7;margin-top:8px}.ima-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.ima-muted{color:#8a9bb4}.ima-cloud-target{display:flex;align-items:center;gap:12px;padding:0 22px 18px}.ima-cloud-target>.v-select{max-width:500px}.ima-cloud-toolbar{display:flex;flex-wrap:wrap;gap:8px;padding:0 22px 18px}.ima-cloud-search{display:flex;gap:12px;align-items:center;padding:0 22px 18px}.ima-cloud-search>.v-text-field{max-width:500px}.ima-folder-link{display:flex;align-items:center;gap:8px;background:none;border:0;color:#245bd6;font:inherit;cursor:pointer;text-align:left;overflow-wrap:anywhere}.ima-settings-body{padding:0 24px}.ima-settings-body section{border-top:1px solid #e8edf5;padding:24px 0}.ima-settings-body h3{font-size:14px;font-weight:650;margin-bottom:16px}.ima-settings-body p,.ima-credentials p{font-size:12px;color:#7589a7;margin:10px 0 16px}.ima-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.ima-target-fields{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.ima-target-fields>.v-select{flex:1;min-width:250px}.ima-checks{display:flex;gap:24px;flex-wrap:wrap;margin-top:10px}.ima-auto-settings{display:flex;align-items:center;justify-content:space-between;gap:20px}.ima-auto-settings>.v-text-field{flex:0 0 260px}.ima-save-bar{display:flex;justify-content:space-between;align-items:center;border-top:1px solid #e8edf5;padding:20px 0;font-size:12px;color:#7589a7}.ima-dirty-text{color:#a76513}.ima-credentials{border-top:1px solid #e8edf5;box-shadow:none!important}.ima-credentials :deep(.v-expansion-panel){box-shadow:none!important}.ima-log-lines{max-height:55vh;overflow:auto;padding:4px 24px 20px;min-height:150px}.ima-log-lines p{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.9 Consolas,monospace;border-bottom:1px solid #edf2f9;padding:6px 0}.ima-log-error{color:#bf3838}.ima-panel :deep(.v-table__wrapper){overflow:auto;max-height:490px}.ima-panel :deep(.v-card){max-width:100%}
@media(max-width:1050px){.ima-workflow{gap:12px}.ima-step{padding:18px 14px;gap:9px}.ima-step h2{font-size:14px}.ima-target-state{padding-left:16px}.ima-target{gap:14px}.ima-task-banner{flex-wrap:wrap}.ima-task-banner>div{flex-basis:100%}}
@media(max-width:700px){.ima-target{padding:16px;flex-wrap:wrap}.ima-target-main{flex-basis:100%;gap:8px}.ima-target-main>span:first-child{flex-basis:100%}.ima-target-state{border:0;padding:0;flex:1}.ima-sections{gap:22px;margin:10px 0 18px}.ima-sections button{font-size:13px}.ima-workflow{grid-template-columns:1fr;gap:10px;margin-bottom:16px}.ima-step{padding:15px;align-items:center}.ima-step>div{display:grid;grid-template-columns:minmax(0,1fr) auto;column-gap:12px;flex:1;min-width:0}.ima-step h2{font-size:14px}.ima-step p{grid-row:2;margin:3px 0 0}.ima-step .v-btn{grid-column:2;grid-row:1/3}.ima-panel-heading{padding:18px 16px}.ima-filters{padding:0 16px 16px;flex-wrap:wrap;gap:12px}.ima-filters>.v-input{flex:1 1 100%!important}.ima-inline-alert{margin:0 16px 16px}.ima-result-meta{padding:0 16px 16px}.ima-auto-bar{padding:16px;align-items:flex-start;flex-direction:column;margin-top:16px}.ima-auto-bar>.ima-actions{width:100%}.ima-auto-bar .v-btn{flex:1}.ima-task-banner{padding:13px 16px}.ima-settings-body{padding:0 16px}.ima-fields{grid-template-columns:1fr}.ima-target-fields{align-items:stretch}.ima-target-fields>.v-select{flex-basis:100%;min-width:0}.ima-auto-settings{align-items:stretch;flex-direction:column}.ima-auto-settings>.v-text-field{flex-basis:auto}.ima-cloud-target,.ima-cloud-toolbar,.ima-cloud-search{padding:0 16px 16px}.ima-cloud-target{flex-wrap:wrap}.ima-cloud-target>.v-select{flex-basis:100%}.ima-cloud-search{flex-wrap:wrap}.ima-cloud-search>.v-text-field{flex-basis:100%}.ima-checks{gap:8px}.ima-checks>.v-input{flex-basis:100%}}
.ima-results[data-kind=plan] :deep(table){min-width:980px}.ima-results[data-kind=plan] :deep(th:first-child){width:25%}.ima-results[data-kind=plan] :deep(th:nth-child(2)){width:29%}.ima-results[data-kind=plan] :deep(th:nth-child(3)){width:110px}.ima-results[data-kind=plan] :deep(th:nth-child(4)){width:25%}.ima-results[data-kind=plan] :deep(th:nth-child(5)){width:76px}
</style>
