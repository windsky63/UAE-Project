<script setup>
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { useDisplay } from 'vuetify'
import ActionIcon from './ActionIcon.vue'
import HelpTip from './HelpTip.vue'
import ConfigPanel from './ConfigPanel.vue'
import FilesPanel from './FilesPanel.vue'
import LogsPanel from './LogsPanel.vue'
import PublicationsPanel from './PublicationsPanel.vue'
import HistoryCleanupPanel from './HistoryCleanupPanel.vue'
import AnalysisCacheCleanupPanel from './AnalysisCacheCleanupPanel.vue'
const ImaPanel = defineAsyncComponent(() => import('./ImaPanel.vue'))
const CodexPanel = defineAsyncComponent(() => import('./CodexPanel.vue'))

const { mobile } = useDisplay()
const drawer = ref(!mobile.value)
const navigation = [
  { id:'overview',title:'工作台',icon:'mdi-view-dashboard-outline',description:'统一查看下载、解析、上传与微信任务。' },
  { id:'codex',title:'Codex 对话',icon:'mdi-code-braces',description:'流式对话、执行过程和文件记录。' },
  { id:'sync',title:'FusionLive 同步',icon:'mdi-cloud-download-outline',description:'管理平台下载、后台解析与每日管道简报。' },
  { id:'nas',title:'绿联云上传',icon:'mdi-cloud-upload-outline',description:'管理已完成下载文件的 NAS 同步任务。' },
  { id:'ima',title:'ima 知识库',icon:'mdi-book-open-page-variant-outline',description:'复制台账资料、管理知识库同步及旧版归档。' },
  { id:'wechat',title:'微信与 Codex',icon:'mdi-wechat',description:'管理本机监听、会话回复和项目文件授权。' },
  { id:'files',title:'文件资料',icon:'mdi-folder-search-outline',description:'检索已下载资料，查看简报与本地模拟结果。' },
  { id:'logs',title:'运行日志',icon:'mdi-text-box-search-outline',description:'集中查看各工具的操作记录与运行输出。' },
]
const page = ref(navigation.some(n=>n.id===location.hash.slice(1))?location.hash.slice(1):'overview')
const current = computed(()=>navigation.find(n=>n.id===page.value))
const info = ref(null)
const failure = ref('')
const loading = ref(false)
const notice = ref('')
const noticeError = ref(false)
const busy = ref('')
const confirmation = ref(null)
const confirmationError = ref('')
const selectedTools = ref([])
const batchTools = computed(()=>info.value?.tools.filter(t=>t.actions?.includes('start')&&t.actions?.includes('stop'))||[])
const allToolsSelected = computed(()=>batchTools.value.length>0&&selectedTools.value.length===batchTools.value.length)
const batchResults = ref([])
const subpage = ref('运行管理')
const tasks = ref([])
const wechatJobs = ref([])
const tool = computed(()=>info.value?.tools.find(t=>t.id===page.value))
const running = computed(()=>info.value?.tools.filter(t=>t.state!=='stopped').length||0)
const downloadDetails = computed(()=>{
  const inventory=info.value?.file_inventory
  if(!inventory)return '本地文件统计暂不可用。'
  const lines=[`正式目录 ${number(inventory.downloads.files)} 个，历史目录 ${number(inventory.history.files)} 个，合计 ${number(inventory.total.files)} 个文件。`,
    '推送文只取同一统计中的正式目录文件；最新版未下载时，正式目录保留最近可用旧版。',
    `待发布缓存 ${number(inventory.pending_publication.files)} 个，单独显示，不计入文件总量。`,
    '打开页面或手动刷新时读取最近生成的日报快照，不扫描目录、不定时轮询。',
    `快照时间：${inventory.generated_at||inventory.scanned_at||'未知'}。文件变化在下次生成快照后反映。`]
  if(inventory.changing)lines.push('目录正在整理，统计可能随迁移继续变化。')
  return lines.join(' ')
})
const labels = {'start':'启动调度','stop':'安全停止','check':'本地检查','briefing':'生成管道简报','generate-text':'生成日报快照','analysis':'恢复后台解析','stop-analysis':'停止后台解析','simulate':'模拟完整流程','collect':'提取本地资料','plan':'预览上传计划','sync-once':'执行一次同步'}
const stateNames = {running:'运行中',external:'外部运行中',stopped:'未启动',succeeded:'已完成',failed:'失败',detached:'待核对',preview:'预览',confirmed:'已发送',pending:'等待',generated:'已生成',not_sent:'未发送',uncertain:'发送待核对',expired:'已过期'}
let version=0

function toast(message,error=false){notice.value=message;noticeError.value=error}
async function api(path, options={}){
  const response=await fetch('/api'+path,{...options,cache:'no-store',headers:{'Content-Type':'application/json','X-Admin-Request':'1',...options.headers}})
  const result=await response.json()
  if(!response.ok)throw Error(result.detail||'请求失败')
  return result
}
async function refresh(){
  const currentVersion=++version
  const requestedPage=page.value
  loading.value=true
  try{
    const result=await api('/overview')
    if(currentVersion===version){info.value=result;failure.value=''}
    if(['sync','nas','wechat'].includes(requestedPage)){
      const result=await api('/tasks?tool='+requestedPage)
      if(currentVersion===version&&requestedPage===page.value)tasks.value=result.items
      if(requestedPage==='wechat'){
        const jobs=await api('/wechat-jobs')
        if(currentVersion===version&&requestedPage===page.value)wechatJobs.value=jobs.items
      }
    }
  }catch(e){if(currentVersion===version)failure.value=e.message}finally{if(currentVersion===version)loading.value=false}
}
async function run(toolId, action){
  busy.value=toolId+action
  try{await api('/action',{method:'POST',body:JSON.stringify({tool:toolId,action})});toast(labels[action]+'已提交，可在任务记录或日志中查看结果。');await refresh()}
  catch(e){toast(e.message,true)}finally{busy.value=''}
}
function confirmOperation(kind, toolId=''){
  confirmationError.value=''
  selectedTools.value=batchTools.value.map(t=>t.id)
  confirmation.value={kind,tool:toolId}
}
async function executeConfirmed(){
  if(busy.value||!confirmation.value)return
  const operation=confirmation.value
  if(operation.kind!=='clear-tasks'&&!selectedTools.value.length){confirmationError.value='请至少选择一个工具。';return}
  busy.value=operation.kind
  confirmationError.value=''
  try{
    if(operation.kind==='clear-tasks'){
      const result=await api('/tasks/clear',{method:'POST',body:JSON.stringify({tool:operation.tool})})
      toast(`已清除 ${result.deleted} 条操作记录及 ${result.deleted_logs} 个对应日志；保留 ${result.skipped} 条运行中记录。待核对记录的日志仍保留。`+(result.errors.length?` ${result.errors.length} 条清理失败。`:''),!!result.errors.length)
    }else{
      const result=await api('/actions/all',{method:'POST',body:JSON.stringify({action:operation.kind,tools:selectedTools.value})})
      batchResults.value=result.items
      toast(operation.kind==='start'?'所选工具的启动请求已处理，请查看各工具结果。':'所选工具的安全关闭请求已处理，工具完成收尾后退出，请查看各工具结果。',result.items.some(item=>item.state==='failed'))
    }
    confirmation.value=null
    await refresh()
  }catch(e){confirmationError.value=e.message}finally{busy.value=''}
}
function go(id){page.value=id;if(mobile.value)drawer.value=false}
function number(n){return n===null||n===undefined?'—':Number(n).toLocaleString('zh-CN')}
function stamp(value){return value?new Date(value).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'—'}
function stateColor(state){return ['running','external','succeeded','confirmed'].includes(state)?'success':state==='failed'?'error':'secondary'}
watch(page,()=>{location.hash=page.value;subpage.value='运行管理';refresh()})
function hashChanged(){const id=location.hash.slice(1);if(navigation.some(n=>n.id===id))page.value=id}
onMounted(()=>{window.addEventListener('hashchange',hashChanged);refresh()})
onUnmounted(()=>{window.removeEventListener('hashchange',hashChanged);version++})
</script>

<template>
  <v-app>
    <v-navigation-drawer v-model="drawer" :temporary="mobile" :permanent="!mobile" width="248" class="workspace-drawer">
      <div class="workspace-brand d-flex align-center ga-3 pa-6">
        <v-avatar color="primary" rounded="lg" size="42"><v-icon icon="mdi-layers-triple-outline"/></v-avatar>
        <div><strong>阿联酋项目</strong><div class="text-caption">工具管理中心</div></div>
      </div>
      <div class="nav-caption">项目工作空间</div>
      <v-list nav>
        <v-list-item v-for="item in navigation" :key="item.id" :active="page===item.id" :prepend-icon="item.icon" :title="item.title" @click="go(item.id)"/>
      </v-list>
      <template #append><div class="pa-5 local-indicator"><span class="status-dot"/> 本机工作空间<div class="text-caption text-secondary mt-1">北京时间 · 数据来自本地工具</div></div></template>
    </v-navigation-drawer>
    <v-app-bar flat height="64" class="workspace-topbar" border="b">
      <v-app-bar-nav-icon v-if="mobile" aria-label="打开导航" @click="drawer=!drawer"/>
      <v-app-bar-title><span>项目工具</span><v-icon icon="mdi-chevron-right" size="16" class="mx-2"/><span>{{current.title}}</span></v-app-bar-title>
      <span class="topbar-time">{{info?.time?stamp(info.time):'连接本机服务中'}}</span>
      <v-btn icon="mdi-refresh" aria-label="刷新状态" :loading="loading" @click="refresh" class="mr-3"/>
    </v-app-bar>
    <v-main>
      <v-container fluid class="workspace-content" :class="{'workspace-chat-content':page==='codex'}">
        <div class="page-heading mb-7">
          <div class="page-identity"><div class="page-symbol"><v-icon :icon="current.icon" size="27"/></div><div><h1>{{current.title}}</h1><p class="text-body-2 text-secondary mt-1">{{current.description}}</p></div></div>
          <div v-if="page==='overview'" class="d-flex flex-wrap ga-3">
            <v-btn color="primary" prepend-icon="mdi-play-outline" :disabled="!!busy||!batchTools.length" @click="confirmOperation('start')">一键启动工具</v-btn>
            <v-btn color="secondary" variant="outlined" prepend-icon="mdi-stop-circle-outline" :disabled="!!busy||!batchTools.length" @click="confirmOperation('stop')">一键安全关闭工具</v-btn>
            <v-btn variant="outlined" prepend-icon="mdi-flask-outline" :disabled="!!busy" :loading="busy==='wechatsimulate'" @click="run('wechat','simulate')">模拟完整流程</v-btn>
          </div>
        </div>
        <v-alert v-if="failure" type="error" variant="tonal" class="mb-5">{{failure}}<div v-if="failure.includes('会话')" class="mt-2">本机可双击“打开管理中心.cmd”；局域网访问请使用服务器“管理中心/runtime/访问地址.txt”中的完整链接。</div></v-alert>
        <KeepAlive><CodexPanel v-if="page==='codex'" :api="api"/></KeepAlive>

        <template v-if="page==='overview'&&info">
          <v-alert v-if="batchResults.length" type="info" variant="tonal" class="mb-5" closable @click:close="batchResults=[]">
            <div v-for="item in batchResults" :key="item.tool">{{info.tools.find(t=>t.id===item.tool)?.name}}：{{item.state==='submitted'?'请求已提交，执行结果见操作记录':item.state==='skipped'?'已跳过：'+item.detail:'提交失败：'+item.detail}}</div>
          </v-alert>
          <v-row class="overview-grid mb-5">
            <v-col cols="12" sm="4"><v-card class="pa-5 metric-card"><div class="metric-caption"><span>运行中的工具</span><v-icon icon="mdi-pulse"/></div><div class="metric-value">{{running}} <small>/ {{info.tools.length}}</small></div><p class="metric-note">{{running?'后台任务正在运行':'当前没有工具运行，按需启动任务'}}</p></v-card></v-col>
            <v-col cols="12" sm="4"><v-card class="pa-5 metric-card"><div class="metric-caption"><span class="metric-title">本地文件总量<HelpTip :text="downloadDetails" label="查看正式目录、历史目录和待发布文件说明"/></span><v-icon icon="mdi-file-check-outline"/></div><div class="metric-value">{{info.file_inventory?number(info.file_inventory.total.files):'—'}} <small>个文件</small></div><p class="metric-note">{{info.file_inventory?'正式 '+number(info.file_inventory.downloads.files)+' · 历史 '+number(info.file_inventory.history.files)+' · 待发布 '+number(info.file_inventory.pending_publication.files):'统计暂不可用'}}</p></v-card></v-col>
            <v-col cols="12" sm="4"><v-card class="pa-5 metric-card"><div class="metric-caption"><span>后台解析任务</span><v-icon icon="mdi-vector-polyline"/></div><div class="metric-value">{{number(info.analysis?.done)}} <small>已完成</small></div><p class="metric-note">{{info.analysis?'等待 '+number(info.analysis.pending||0)+' · 处理中 '+number(info.analysis.running||0)+' · 失败 '+number(info.analysis.failed||0):'本机尚无解析队列数据库'}}</p></v-card></v-col>
          </v-row>
          <v-card class="mb-6">
            <div class="section-toolbar pa-5"><div><h2>工具运行状态</h2><p class="section-subtitle">{{info.tools.length}} 个工具独立运行，统一查看进度与操作。</p></div><v-chip size="small" variant="tonal" color="primary">本地管理</v-chip></div>
            <div class="tool-list">
              <article v-for="item in info.tools" :key="item.id" class="tool-row">
                <v-avatar color="primary" variant="tonal" rounded="lg" size="46"><v-icon :icon="item.icon" size="25"/></v-avatar>
                <div class="tool-info"><strong>{{item.name}}</strong><p>{{item.description}}</p></div>
                <div class="tool-schedule"><v-icon icon="mdi-clock-outline" size="15"/> {{item.schedule}}</div>
                <v-chip :color="stateColor(item.state)" size="small" variant="tonal"><span class="chip-dot"/>{{stateNames[item.state]||item.state}}</v-chip>
                <v-btn variant="text" append-icon="mdi-arrow-right" @click="go(item.id)">管理</v-btn>
              </article>
            </div>
          </v-card>
          <v-card>
            <div class="section-toolbar pa-5"><div><h2>最近任务</h2><p class="section-subtitle">从管理中心发起的操作及其执行结果。</p></div><v-btn variant="text" prepend-icon="mdi-text-box-search-outline" @click="go('logs')">查看运行日志</v-btn></div>
            <v-table v-if="info.tasks.length"><thead><tr><th>工具 / 操作</th><th>状态</th><th>开始时间</th><th>结束时间</th></tr></thead><tbody><tr v-for="task in info.tasks" :key="task.id"><td><strong>{{info.tools.find(t=>t.id===task.tool)?.name}}</strong><p class="text-caption text-secondary">{{task.label}}</p></td><td><v-chip size="small" :color="stateColor(task.state)">{{stateNames[task.state]||task.state}}</v-chip></td><td>{{stamp(task.started)}}</td><td>{{stamp(task.ended)}}</td></tr></tbody></v-table>
            <div v-else class="empty-state"><v-icon icon="mdi-clipboard-text-clock-outline" size="38" color="secondary"/><h3 class="mt-3">还没有管理任务</h3><p class="mt-2">从工具页面启动任务，或先运行一次本地模拟。</p></div>
          </v-card>
        </template>

        <ImaPanel v-else-if="page==='ima'" :api="api" @notice="toast"/>
        <template v-else-if="tool">
          <v-card class="mb-5 pa-5 tool-summary"><div class="d-flex align-center ga-3"><v-chip :color="stateColor(tool.state)">{{stateNames[tool.state]}}</v-chip><span class="text-body-2 text-secondary"><v-icon icon="mdi-clock-outline" size="16"/> {{tool.schedule}}</span></div><span class="text-caption text-secondary">{{tool.description}}</span></v-card>
          <div class="panel-tabs mb-5"><button v-for="name in ['运行管理','配置设置',...(page==='sync'?['每日推送']:[]),...(page==='wechat'?['回复任务']:[])]" :key="name" :class="{selected:subpage===name}" @click="subpage=name">{{name}}</button></div>
          <ConfigPanel v-if="subpage==='配置设置'" :key="page" :tool="page" :api="api" @notice="toast"/>
          <PublicationsPanel v-else-if="page==='sync'&&subpage==='每日推送'" :api="api" @notice="toast"/>
          <v-card v-else-if="subpage==='回复任务'">
            <div class="pa-5"><h2>最近回复任务</h2><p class="section-subtitle">预览、生成、发送与异常结果来自本地监听数据库。</p></div>
            <v-table v-if="wechatJobs.length"><thead><tr><th>会话</th><th>状态</th><th>最终回复</th><th>状态说明</th><th>创建时间</th></tr></thead><tbody><tr v-for="job in wechatJobs" :key="job.id"><td>{{job.chat_id}}</td><td>{{stateNames[job.status]||job.status}}</td><td class="reply-cell">{{job.reply||'—'}}</td><td class="reply-cell">{{job.error||'—'}}</td><td>{{stamp(job.created_at)}}</td></tr></tbody></v-table>
            <div v-else class="empty-state">本机尚无回复任务记录。</div>
          </v-card>
          <template v-else>
            <v-card class="mb-6 pa-5"><h2>任务操作</h2><p class="section-subtitle mb-5">{{page==='sync'?'下载调度按北京时间运行；后台解析与简报可独立执行。':page==='nas'?'使用已有配置上传到 NAS；本地检查只验证程序完整性。':'监听使用已保存的聊天和发送配置；模拟流程只生成本地预览。'}}</p><div class="d-flex flex-wrap ga-3"><v-btn v-for="action in tool.actions.filter(value=>!['clean-history','stop-history-cleanup','clean-analysis-cache'].includes(value))" :key="action" :color="action==='stop'||action==='stop-analysis'?'secondary':action==='start'?'primary':undefined" :variant="action==='start'?'flat':'outlined'" :prepend-icon="action==='start'?'mdi-play-outline':action.startsWith('stop')?'mdi-stop-circle-outline':action==='check'?'mdi-shield-check-outline':action==='simulate'?'mdi-flask-outline':action==='briefing'?'mdi-file-chart-outline':'mdi-vector-polyline'" :loading="busy===page+action" :disabled="!!busy||(action==='start'&&tool.state!=='stopped')" @click="run(page,action)">{{page==='wechat'&&action==='start'?'启动监听':labels[action]}}</v-btn></div></v-card>
            <HistoryCleanupPanel v-if="page==='sync'&&tool.actions.includes('clean-history')" :api="api" @notice="toast"/>
            <AnalysisCacheCleanupPanel v-if="page==='sync'&&tool.actions.includes('clean-analysis-cache')" :api="api" @notice="toast"/>
            <v-card><div class="section-toolbar pa-5"><h2>操作记录</h2><v-btn variant="text" color="error" prepend-icon="mdi-delete-outline" :disabled="!!busy||!tasks.length" @click="confirmOperation('clear-tasks',page)">清除操作记录</v-btn><v-btn variant="text" @click="go('logs')">查看日志</v-btn></div><v-table v-if="tasks.length"><thead><tr><th>操作</th><th>状态</th><th>开始时间</th><th>退出码</th></tr></thead><tbody><tr v-for="task in tasks" :key="task.id"><td><strong>{{task.label}}</strong></td><td><v-chip size="small" :color="stateColor(task.state)">{{stateNames[task.state]||task.state}}</v-chip></td><td>{{stamp(task.started)}}</td><td>{{task.exit_code??'—'}}</td></tr></tbody></v-table><div v-else class="empty-state">尚未从管理中心发起此工具的操作。</div></v-card>
          </template>
        </template>
        <FilesPanel v-else-if="page==='files'" :api="api" @notice="toast"/>
        <LogsPanel v-else-if="page==='logs'" :api="api" @notice="toast"/>
      </v-container>
    </v-main>
    <v-dialog :model-value="!!confirmation" @update:model-value="value=>{if(!value&&!busy)confirmation=null}" max-width="560" :persistent="!!busy">
      <v-card :title="confirmation?.kind==='clear-tasks'?'清除操作记录':confirmation?.kind==='start'?'选择要启动的工具':'选择要安全关闭的工具'">
        <v-card-text>
          <p v-if="confirmation?.kind==='clear-tasks'">确认清除{{info?.tools.find(t=>t.id===confirmation.tool)?.name}}的已结束操作记录及对应任务日志？删除后无法恢复。运行中任务保留；待核对记录可清除，但保留日志，不终止进程，工具台账、配置、解析缓存和微信回复记录会保留。</p>
          <template v-else>
            <p>{{confirmation?.kind==='start'?'默认全选，可取消不需要启动的工具。使用已保存配置，已运行的工具会跳过。':'默认全选，可取消不需要关闭的工具。所选工具完成收尾后退出，可能需要等待。管理中心保持运行。'}}</p>
            <div class="batch-selection-toolbar">
              <v-checkbox :model-value="allToolsSelected" :indeterminate="selectedTools.length>0&&!allToolsSelected" :disabled="!!busy" label="全选工具" hide-details density="compact" @update:model-value="value=>selectedTools=value?batchTools.map(t=>t.id):[]"/>
              <span aria-live="polite">已选 {{selectedTools.length}} / {{batchTools.length}} 个工具</span>
            </div>
            <div class="batch-tool-list">
              <v-checkbox v-for="item in batchTools" :key="item.id" v-model="selectedTools" :value="item.id" :label="item.name" :disabled="!!busy" hide-details class="batch-tool-choice">
                <template #label><div class="batch-tool-label"><strong>{{item.name}}</strong><span>{{item.description}}</span><v-chip :color="stateColor(item.state)" size="x-small" variant="tonal">{{stateNames[item.state]||item.state}}</v-chip></div></template>
              </v-checkbox>
            </div>
            <p v-if="!selectedTools.length" class="text-error mt-3" role="status">请至少选择一个工具。</p>
            <v-alert v-if="confirmation?.kind==='start'&&(selectedTools.includes('wechat')||selectedTools.includes('ima'))" type="warning" variant="tonal" class="mt-4">{{selectedTools.includes('wechat')?'微信可能根据已保存配置发送回复。':''}} {{selectedTools.includes('ima')?'ima 会上传资料并归档旧版。':''}}</v-alert>
          </template>
          <v-alert v-if="confirmationError" type="error" variant="tonal" class="mt-4">{{confirmationError}}</v-alert>
        </v-card-text>
        <v-card-actions><v-spacer/><v-btn :disabled="!!busy" @click="confirmation=null">取消</v-btn><v-btn color="primary" :loading="!!busy" :disabled="!!busy||(confirmation?.kind!=='clear-tasks'&&!selectedTools.length)" @click="executeConfirmed">{{confirmation?.kind==='clear-tasks'?'确认执行':confirmation?.kind==='start'?'启动所选工具':'安全关闭所选工具'}}</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
    <v-snackbar :model-value="!!notice" @update:model-value="notice=''" :color="noticeError?'error':'success'" :timeout="6000">{{notice}}</v-snackbar>
  </v-app>
</template>
