<script setup>
import {computed,nextTick,onActivated,onMounted,onUnmounted,ref} from 'vue'
import MessageText from './MessageText.vue'
import { requestId, copyText } from './browser.js'
const props=defineProps({api:Function})
const notice=ref('')
const sessions=ref([]), current=ref(null), messages=ref([]), events=ref([]), draft=ref(''), error=ref('')
const media=window.matchMedia('(max-width:900px)')
const narrow=ref(media.matches)
const sending=ref(false), selecting=ref(false), historyOpen=ref(!media.matches), filesOpen=ref(false), settingsOpen=ref(false), connected=ref(false)
const sessionView=ref('active'),renameTarget=ref(null),renameTitle=ref(''),sessionBusy=ref(false)
const transport=ref('inherit'), modelConnection=ref('')
const model=ref(''), effort=ref('medium'), mode=ref('read-only'), options=ref({workspace:'',available:false})
const scroller=ref(null), composer=ref(null), expanded=ref(new Set())
let stream, loadVersion=0, listVersion=0, autoscroll=true
const working=computed(()=>['running','stopping'].includes(current.value?.status))
const readOnlySession=computed(()=>!!(current.value?.archived_at||current.value?.deleted_at||current.value?.source==='wechat'))
const purgeTarget=ref(null)
const statusLabels={record:'问答记录',idle:'准备就绪',running:'正在处理',stopping:'正在停止',completed:'已完成',failed:'执行失败',interrupted:'已停止'}
const steps=computed(()=>{
  const items=new Map()
  for(const e of events.value){
    const p=e.payload
    if(e.kind==='item_started'||e.kind==='item_completed'){
      if(p.type==='agentMessage')continue
      items.set(p.id,{...items.get(p.id),...p,complete:e.kind==='item_completed',seq:items.get(p.id)?.seq||e.seq})
    }else if(e.kind==='summary_delta'){
      const prior=items.get(p.id)||{id:p.id,type:'reasoning',summary:[],seq:e.seq}
      if(!prior.complete){const summary=[...(prior.summary||[])];summary[p.summary_index]=(summary[p.summary_index]||'')+p.delta;items.set(p.id,{...prior,summary})}
    }else if(e.kind==='command_output'){
      const prior=items.get(p.id)||{id:p.id,type:'commandExecution',seq:e.seq}
      items.set(p.id,{...prior,aggregatedOutput:(prior.aggregatedOutput||'')+p.delta})
    }else if(e.kind==='plan')items.set('plan-'+e.seq,{id:'plan-'+e.seq,type:'plan',text:p.plan.map(x=>(x.status==='completed'?'✓ ':'○ ')+x.step).join('\n'),seq:e.seq,complete:true})
    else if(e.kind==='error')items.set('error-'+e.seq,{id:'error-'+e.seq,type:'error',text:p.message,seq:e.seq,complete:true})
  }
  return [...items.values()].sort((a,b)=>a.seq-b.seq)
})
const fileRecords=computed(()=>{
  const files=new Map()
  for(const step of steps.value){
    if(step.type==='commandExecution')for(const a of step.commandActions||[]){
      if(a.path&&['read','search','listFiles'].includes(a.type))files.set(a.path+':'+a.type,{path:a.path,operation:{read:'读取',search:'搜索范围',listFiles:'目录列表'}[a.type],state:step.complete?(step.exitCode===0?'完成':'失败'):'执行中',id:step.id})
    }
    if(step.type==='fileChange')for(const c of step.changes||[]){
      const kind=typeof c.kind==='object'?c.kind.type:c.kind
      files.set(c.path+':write',{path:c.path,operation:kind==='delete'?'删除':'修改',state:step.complete&&step.status==='completed'?'已应用':'待确认',id:step.id})
    }
    if(step.type==='imageView'&&step.path)files.set(step.path+':image',{path:step.path,operation:'查看图像',state:step.complete?'完成':'执行中',id:step.id})
  }
  for(const e of events.value.filter(e=>e.kind==='connected'))for(const f of e.payload.instruction_files||[]){const path=typeof f==='string'?f:f.path;if(path)files.set(path+':instruction',{path,operation:'指令文件',state:'已载入'})}
  return [...files.values()]
})
const latestDiff=computed(()=>events.value.filter(e=>e.kind==='diff').at(-1)?.payload.diff||'')
const usage=computed(()=>events.value.filter(e=>e.kind==='usage').at(-1)?.payload.usage)
const hasContent=computed(()=>messages.value.length||steps.value.length)
const timeline=computed(()=>{
  const entries=messages.value.map((message,index)=>{
    const first=events.value.find(e=>(e.kind==='user_message'&&e.payload.id===message.id)||
      (e.kind==='message_delta'&&e.payload.id===message.id)||
      ((e.kind==='item_started'||e.kind==='item_completed')&&e.payload.type==='agentMessage'&&message.id.endsWith(':agent:'+e.payload.id)))
    return {key:message.id,message,order:first?.seq??index+.1}
  })
  entries.push(...steps.value.map(step=>({key:'step-'+step.id,step,order:step.seq})))
  return entries.sort((a,b)=>a.order-b.order)
})

function toggle(set,id){const n=new Set(set.value);n.has(id)?n.delete(id):n.add(id);set.value=n}
function toggleStep(id){toggle(expanded,id)}
function label(step){
  if(step.type==='reasoning')return '推理摘要'
  if(step.type==='commandExecution'){const a=step.commandActions?.[0];return a?.type==='read'?'读取 '+(a.name||a.path?.split(/[\\/]/).at(-1)):a?.type==='search'?'搜索 '+(a.query||a.path||'项目文件'):a?.type==='listFiles'?'列出 '+(a.path||'项目目录'):'运行命令'}
  return {fileChange:'文件变更',mcpToolCall:'调用 '+(step.server||'')+' / '+(step.tool||''),dynamicToolCall:'调用 '+(step.tool||''),plan:'执行计划',webSearch:'搜索网页',imageView:'查看图像',error:'执行错误'}[step.type]||'执行步骤'
}
function icon(step){return step.type==='reasoning'?'mdi-lightbulb-outline':step.type==='commandExecution'?'mdi-console':step.type==='fileChange'?'mdi-file-edit-outline':step.type==='error'?'mdi-alert-circle-outline':'mdi-tools'}
function stepText(step){if(step.type==='reasoning')return(step.summary||[]).join('\n\n');if(step.type==='commandExecution')return step.command||'';if(step.type==='fileChange')return(step.changes||[]).map(c=>c.path+'\n'+(c.diff||'')).join('\n\n');return step.text||step.query||JSON.stringify(step.arguments||step.result||{},null,2)}
async function scroll(){await nextTick();if(autoscroll&&scroller.value)scroller.value.scrollTop=scroller.value.scrollHeight}
function onScroll(){const s=scroller.value;if(s)autoscroll=s.scrollHeight-s.scrollTop-s.clientHeight<100}
function receive(event){
  if(events.value.some(e=>e.seq===event.seq))return
  events.value.push(event)
  const p=event.payload
  if(event.kind==='connection_status')modelConnection.value=p.state
  if(['turn_started','message_delta','summary_delta','item_started','item_completed','status'].includes(event.kind))modelConnection.value=''
  if(event.kind==='notice')notice.value=p.message
  if(event.kind==='user_message'){if(!messages.value.some(m=>m.id===p.id))messages.value.push(p);if(current.value?.title==='新对话')current.value.title=p.text.trim().replace(/\n/g,' ').slice(0,45)}
  if(event.kind==='message_delta'){
    let message=messages.value.find(m=>m.id===p.id)
    if(!message){message={id:p.id,role:'assistant',text:''};messages.value.push(message)}
    message.text+=p.delta
  }
  if(event.kind==='item_completed'&&p.type==='agentMessage'){
    let message=messages.value.find(m=>m.id===p.message_id)
    if(!message)messages.value.push({id:p.message_id,role:'assistant',text:p.text||''});else message.text=p.text||''
  }
  if(event.kind==='status'&&current.value){current.value.status=p.status;if(!['running','stopping'].includes(p.status))loadSessions()}
  if(event.kind==='error')error.value=p.message
  scroll()
}
async function loadSessions(){const version=++listVersion,view=sessionView.value;try{const result=await props.api('/codex/sessions?view='+view);if(version===listVersion&&view===sessionView.value){sessions.value=result.items;if(result.warning)notice.value=result.warning}}catch(e){error.value=e.message}}
async function select(id){
  const version=++loadVersion
  selecting.value=true
  stream?.close();connected.value=false;error.value=''
  try{
    const result=await props.api('/codex/session?id='+encodeURIComponent(id))
    if(version!==loadVersion)return
    current.value=result.session;messages.value=result.messages;events.value=result.events
    modelConnection.value=''
    transport.value=result.session.transport||'inherit'
    for(const event of result.events){if(event.kind==='connection_status')modelConnection.value=event.payload.state;else if(['turn_started','message_delta','summary_delta','item_started','item_completed','status'].includes(event.kind))modelConnection.value=''}
    notice.value=result.events.findLast(e=>e.kind==='notice')?.payload.message||''
    model.value=result.session.model;mode.value=result.session.mode;effort.value=result.session.effort
    expanded.value=new Set();draft.value='';if(narrow.value)historyOpen.value=false;autoscroll=true
    try{localStorage.setItem('project-codex-session',id)}catch{}
    if(result.session.source==='wechat'){notice.value='微信 Codex 问答记录，仅供查看；状态标记说明是否已发送。在微信中继续对话，管理页归档不影响微信原始消息。';scroll();return}
    stream=new EventSource('/api/codex/events?id='+id+'&after='+result.last_event_id)
    stream.onmessage=e=>{if(version===loadVersion)receive(JSON.parse(e.data))}
    stream.onopen=()=>{connected.value=true}
    stream.onerror=()=>{connected.value=false}
    scroll()
  }catch(e){error.value=e.message}finally{if(version===loadVersion)selecting.value=false}
}
async function newSession(){
  if(selecting.value||sessionBusy.value)return
  sessionView.value='active'
  if(current.value&&!messages.value.length&&!events.value.length&&!readOnlySession.value){await loadSessions();composer.value?.focus();return}
  selecting.value=true
  try{const result=await props.api('/codex/sessions',{method:'POST',body:JSON.stringify({mode:mode.value,model:model.value,effort:effort.value,transport:transport.value})});await loadSessions();await select(result.session.id);composer.value?.focus()}catch(e){error.value=e.message}finally{selecting.value=false}
}
async function send(){
  const text=draft.value.trim()
  if(!text||sending.value||selecting.value||working.value||readOnlySession.value)return
  sending.value=true;error.value='';autoscroll=true
  try{
    if(!current.value)await newSession()
    if(!current.value)throw Error('无法创建对话')
    await props.api('/codex/configure',{method:'POST',body:JSON.stringify({session_id:current.value.id,mode:mode.value,model:model.value,effort:effort.value,transport:transport.value})})
    current.value.status='running'
    await props.api('/codex/send',{method:'POST',body:JSON.stringify({session_id:current.value.id,text,request_id:requestId()})})
    draft.value='';settingsOpen.value=false;await loadSessions()
  }catch(e){error.value=e.message;draft.value=text;if(current.value){try{current.value=(await props.api('/codex/session?id='+current.value.id)).session}catch{}}}finally{sending.value=false;scroll()}
}
async function stop(){try{await props.api('/codex/stop',{method:'POST',body:JSON.stringify({session_id:current.value.id})})}catch(e){error.value=e.message}}
function keydown(e){if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send()}}
async function copy(text){try{await copyText(text)}catch{error.value='浏览器暂不允许复制，请手动选择文本。'}}
function openFile(f){filesOpen.value=false;if(f.id){expanded.value=new Set([...expanded.value,f.id]);nextTick(()=>document.getElementById('step-'+f.id)?.scrollIntoView({block:'center',behavior:'smooth'}))}}
function resetCurrent(){loadVersion++;stream?.close();current.value=null;messages.value=[];events.value=[];draft.value='';connected.value=false;modelConnection.value='';try{localStorage.removeItem('project-codex-session')}catch{}}
async function manageSession(session,action,title=''){
  if(sessionBusy.value)return
  sessionBusy.value=true;error.value=''
  try{
    await props.api('/codex/session-action',{method:'POST',body:JSON.stringify({session_id:session.id,action,title})})
    if(action==='purge')purgeTarget.value=null
    if(action==='rename'){if(current.value?.id===session.id)current.value.title=title;renameTarget.value=null;await loadSessions();return}
    const wasCurrent=current.value?.id===session.id
    await loadSessions()
    if(wasCurrent){resetCurrent();if(sessions.value.length)await select(sessions.value[0].id)}
  }catch(e){error.value=e.message}finally{sessionBusy.value=false}
}
function renameSession(session){renameTarget.value=session;renameTitle.value=session.title}
function mediaChanged(event){narrow.value=event.matches;historyOpen.value=!event.matches}
onMounted(async()=>{
  media.addEventListener('change',mediaChanged)
  try{options.value=await props.api('/codex/options');model.value=options.value.model||'';await loadSessions();const old=localStorage.getItem('project-codex-session');if(sessions.value.some(s=>s.id===old))await select(old)}catch(e){error.value=e.message}
})
onActivated(async()=>{try{options.value=await props.api('/codex/options')}catch{};scroll()})
onUnmounted(()=>{media.removeEventListener('change',mediaChanged);loadVersion++;stream?.close()})
</script>

<template>
  <div class="codex-shell" data-testid="codex-panel">
    <v-alert v-if="notice" type="info" variant="tonal" density="compact" closable class="ma-2 flex-shrink-0" @click:close="notice=''">{{notice}}</v-alert>
    <div class="codex-layout">
      <aside v-if="historyOpen" class="codex-session-list" aria-label="对话列表">
        <div class="codex-session-heading"><strong>对话列表</strong><button aria-label="刷新会话" :disabled="sessionBusy||selecting" @click="loadSessions"><v-icon icon="mdi-refresh" size="19"/></button><button aria-label="新建对话" title="新建对话" :disabled="selecting||sessionBusy" @click="newSession"><v-icon icon="mdi-plus" size="19"/></button></div>
        <v-select v-model="sessionView" :items="[{title:'常用对话',value:'active'},{title:'已归档',value:'archived'},{title:'回收站',value:'trash'}]" :disabled="sessionBusy||selecting" aria-label="会话列表分类" class="codex-session-view" density="compact" variant="outlined" hide-details @update:model-value="loadSessions"/>
        <div class="codex-session-rows"><div v-for="session in sessions" :key="session.id" class="codex-session-row" :class="{active:session.id===current?.id}"><button class="codex-session-select" :disabled="sessionBusy||selecting" @click="select(session.id)"><span>{{session.title}}</span><small>{{session.source==='wechat'?'\u5fae\u4fe1':'\u7f51\u9875'}}·{{statusLabels[session.status]||session.status}} · {{new Date(session.updated).toLocaleDateString('zh-CN')}}</small></button><v-menu><template #activator="{props:menuProps}"><button v-bind="menuProps" :aria-label="'操作对话 '+session.title" class="codex-session-more" :disabled="sessionBusy||selecting"><v-icon icon="mdi-dots-horizontal" size="17"/></button></template><v-list density="compact" class="codex-session-actions"><v-list-item title="重命名" @click="renameSession(session)"/><v-list-item v-if="sessionView==='active'" title="归档" :disabled="sessionBusy||['running','stopping'].includes(session.status)" @click="manageSession(session,'archive')"/><v-list-item v-if="sessionView==='archived'" title="取消归档" :disabled="sessionBusy" @click="manageSession(session,'unarchive')"/><v-list-item v-if="sessionView==='trash'" title="恢复对话" :disabled="sessionBusy" @click="manageSession(session,'restore')"/><v-list-item v-else title="删除（移入回收站）" :disabled="sessionBusy||['running','stopping'].includes(session.status)" @click="manageSession(session,'delete')"/><v-list-item v-if="sessionView==='trash'" title="彻底删除" :disabled="sessionBusy" @click="purgeTarget=session"/></v-list></v-menu></div><p v-if="!sessions.length" class="codex-drawer-empty">{{sessionView==='active'?'尚无对话，发送消息即可开始。':sessionView==='archived'?'暂无归档对话。':'回收站为空。'}}</p></div>
      </aside>
      <section class="codex-conversation">
    <div class="codex-toolbar"><button class="codex-title-button" title="历史会话" @click="historyOpen=!historyOpen"><v-icon icon="mdi-chevron-left" size="17"/><span>{{current?.title||'新对话'}}</span></button><div class="codex-toolbar-actions"><button aria-label="文件记录" title="文件记录" @click="filesOpen=!filesOpen"><v-icon icon="mdi-file-document-multiple-outline" size="17"/><span v-if="fileRecords.length" class="codex-file-count">{{fileRecords.length}}</span></button><button aria-label="历史会话" title="历史会话" @click="historyOpen=!historyOpen"><v-icon icon="mdi-history" size="18"/></button><button aria-label="新建对话" title="新建对话" @click="newSession"><v-icon icon="mdi-square-edit-outline" size="17"/></button></div></div>
    <main ref="scroller" class="codex-scroll" @scroll="onScroll">
      <div class="codex-thread">
        <div v-if="!hasContent" class="codex-empty"><v-icon icon="mdi-code-braces" size="36"/><h1>接下来做什么？</h1><p>在阿联酋项目中使用 Codex</p><div class="codex-suggestions"><button v-for="suggestion in ['核对管道简报的统计逻辑','查看下载文件和解析结果','检查三个工具的本机状态']" :key="suggestion" @click="draft=suggestion;composer?.focus()">{{suggestion}}<v-icon icon="mdi-arrow-top-right" size="15"/></button></div></div>
        <template v-for="entry in timeline" :key="entry.key">
          <template v-if="entry.message"><div v-if="entry.message.role==='user'" class="codex-user"><div>{{entry.message.text}}</div></div><article v-else class="codex-assistant"><MessageText :text="entry.message.text"/><button v-if="entry.message.text" class="codex-copy" aria-label="复制回复" title="复制回复" @click="copy(entry.message.text)"><v-icon icon="mdi-content-copy" size="15"/></button></article></template>
          <section v-else class="codex-process" aria-label="执行过程">
          <div v-if="entry.step.id===steps[0]?.id" class="codex-process-title"><v-icon :icon="working?'mdi-loading':'mdi-check'" :class="{'codex-spin':working}" size="16"/><span>{{working?'正在处理':'执行过程'}} · {{steps.length}} 项</span><span class="codex-summary-hint" title="只展示 Codex 提供的可读摘要和工具事件，不包含模型内部完整思维链。">推理摘要与工具记录</span></div>
          <article v-for="step in [entry.step]" :key="step.id" :id="'step-'+step.id" class="codex-step" :class="{'is-error':step.type==='error'||step.status==='failed'}">
            <button class="codex-step-head" :aria-expanded="expanded.has(step.id)" @click="toggleStep(step.id)"><v-icon :icon="icon(step)" size="15"/><span>{{label(step)}}</span><span v-if="step.type==='commandExecution'&&step.exitCode!==undefined&&step.exitCode!==null" class="codex-step-meta">{{step.exitCode===0?'成功':'退出码 '+step.exitCode}}</span><v-icon :icon="expanded.has(step.id)?'mdi-chevron-down':'mdi-chevron-right'" size="15"/></button>
            <div v-if="step.type==='reasoning'&&!expanded.has(step.id)&&step.summary?.length" class="codex-summary-preview">{{step.summary.join(' ')}}</div>
            <div v-if="expanded.has(step.id)" class="codex-step-body"><MessageText v-if="step.type==='reasoning'||step.type==='plan'" :text="stepText(step)||'此事件未提供可读摘要。'"/><template v-else><pre class="codex-command">{{stepText(step)}}</pre><template v-if="step.type==='commandExecution'"><div class="codex-command-info">{{step.cwd||options.workspace}}</div><details :open="!!step.aggregatedOutput"><summary>命令输出</summary><pre class="codex-output">{{step.aggregatedOutput||'暂无输出'}}</pre></details></template><pre v-if="step.type==='mcpToolCall'&&step.result" class="codex-output">{{JSON.stringify(step.result,null,2)}}</pre></template></div>
          </article>
          </section>
        </template>
        <details v-if="latestDiff" class="codex-diff"><summary>查看文件变更</summary><pre>{{latestDiff}}</pre></details>
        <div v-if="working&&!steps.length" class="codex-working"><v-icon icon="mdi-loading" class="codex-spin" size="16"/> Codex 正在处理…</div>
        <div v-if="current&&!working&&hasContent" class="codex-turn-status">{{statusLabels[current.status]||current.status}}<span v-if="usage?.last?.totalTokens"> · {{usage.last.totalTokens.toLocaleString()}} tokens</span></div>
      </div>
    </main>
    <div class="codex-bottom"><div class="codex-composer-wrap"><div v-if="error" class="codex-error" role="alert"><v-icon icon="mdi-alert-circle-outline" size="16"/><span>{{error}}</span><button aria-label="关闭错误" @click="error=''"><v-icon icon="mdi-close" size="15"/></button></div><div v-if="settingsOpen" class="codex-settings"><v-text-field v-model="model" label="模型" placeholder="留空使用本机默认模型" :disabled="working" aria-label="模型名称" density="compact" hide-details/><v-select v-model="effort" label="推理强度" :items="[{title:'低',value:'low'},{title:'中',value:'medium'},{title:'高',value:'high'},{title:'极高',value:'xhigh'}]" :disabled="working" aria-label="推理强度" density="compact" hide-details/><v-select v-model="transport" label="模型连接方式" :items="[{title:'继承微信与 Codex 设置',value:'inherit'},{title:'跟随 Codex 默认',value:'auto'},{title:'尝试 WebSocket',value:'websocket'},{title:'HTTPS 流式（跳过 WebSocket）',value:'https'}]" :disabled="working" aria-label="模型连接方式" density="compact" hide-details/><p>连接方式下次发送生效；群聊使用“微信与 Codex”中的共享默认值。此处单独选择只影响当前网页会话。</p><details><summary>共享可访问目录（{{options.access_directories?.length||0}}）</summary><p v-for="path in options.access_directories" :key="path">{{path}}</p><p>在“微信与 Codex → 配置设置”中编辑；两处使用同一目录列表。</p></details></div><p v-if="readOnlySession" class="codex-session-readonly">此会话已归档或移入回收站，恢复后可继续对话。</p><form class="codex-composer" @submit.prevent="send"><textarea ref="composer" v-model="draft" :disabled="selecting||readOnlySession" placeholder="询问 Codex，或描述你要完成的任务" aria-label="发送给 Codex 的消息" maxlength="16000" rows="3" @keydown="keydown"/><div class="codex-composer-footer"><div class="codex-composer-left"><button type="button" aria-label="新建空白对话" title="新建对话" @click="newSession"><v-icon icon="mdi-plus" size="20"/></button><v-select v-model="mode" :items="[{title:'询问 · 只读',value:'read-only'},{title:'编辑 · 本项目',value:'workspace-write'}]" aria-label="对话权限" :disabled="working" class="codex-mode-select" variant="plain" density="compact" hide-details :menu-props="{minWidth:200}"/></div><div class="codex-composer-right"><button type="button" class="codex-model" @click="settingsOpen=!settingsOpen"><v-icon icon="mdi-lightning-bolt-outline" size="15"/><span>{{model||options.model||'默认模型'}}</span><v-icon icon="mdi-chevron-down" size="14"/></button><button v-if="working" type="button" class="codex-send codex-stop" aria-label="停止生成" :disabled="current.status==='stopping'" @click="stop"><v-icon icon="mdi-stop" size="17"/></button><button v-else type="submit" class="codex-send" aria-label="发送消息" :disabled="!draft.trim()||sending||selecting||readOnlySession"><v-icon :icon="sending?'mdi-loading':'mdi-arrow-up'" :class="{'codex-spin':sending}" size="20"/></button></div></div></form><div class="codex-context"><span><v-icon icon="mdi-laptop" size="14"/> 在本机工作<span class="codex-workspace">{{options.workspace.split(/[\\/]/).at(-1)||'阿联酋项目'}}</span></span><span v-if="current">{{!connected?'页面连接恢复中…':modelConnection==='reconnecting'?'Codex 正在重连…':'已连接'}}</span><span v-else>Enter 发送 · Shift+Enter 换行</span></div></div></div>
      </section>
    </div>
    <div v-if="filesOpen||(historyOpen&&narrow)" class="codex-shade" @click="historyOpen=false;filesOpen=false"/>
    <aside v-if="filesOpen" class="codex-files"><div class="codex-drawer-title"><h2>文件记录 <span>{{fileRecords.length}}</span></h2><button aria-label="关闭文件记录" @click="filesOpen=false"><v-icon icon="mdi-close" size="18"/></button></div><p class="codex-drawer-note">仅记录工具报告的文件和范围；搜索命中不代表文件已读取。可展开步骤核对具体命令。</p><button v-for="f in fileRecords" :key="f.path+f.operation" class="codex-file-row" @click="openFile(f)"><v-icon icon="mdi-file-document-outline" size="18"/><span><strong>{{f.path.split(/[\\/]/).at(-1)}}</strong><small>{{f.path}}</small><em>{{f.operation}} · {{f.state}}</em></span></button><p v-if="!fileRecords.length" class="codex-drawer-empty">本次对话尚无结构化文件记录。</p></aside>
  </div>
  <v-dialog :model-value="!!renameTarget" @update:model-value="renameTarget=null" max-width="420"><v-card class="pa-5"><h3 class="mb-4">重命名对话</h3><v-text-field v-model="renameTitle" label="会话名称" maxlength="100" autofocus @keydown.enter="manageSession(renameTarget,'rename',renameTitle)"/><div class="d-flex justify-end ga-2 mt-4"><v-btn variant="text" @click="renameTarget=null">取消</v-btn><v-btn color="primary" :disabled="!renameTitle.trim()" :loading="sessionBusy" @click="manageSession(renameTarget,'rename',renameTitle)">保存</v-btn></div></v-card></v-dialog>
  <v-dialog :model-value="!!purgeTarget" @update:model-value="purgeTarget=null" max-width="460" :persistent="sessionBusy"><v-card title="彻底删除会话"><v-card-text>确认彻底删除“{{purgeTarget?.title}}”？此处消息和执行记录无法恢复。微信原始消息及发送审计记录保留，避免影响监听与去重。</v-card-text><v-card-actions><v-spacer/><v-btn :disabled="sessionBusy" @click="purgeTarget=null">取消</v-btn><v-btn color="error" :loading="sessionBusy" @click="manageSession(purgeTarget,'purge')">确认彻底删除</v-btn></v-card-actions></v-card></v-dialog>
</template>

<style src="./codex.css"></style>
