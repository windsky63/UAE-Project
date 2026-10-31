<script setup>
import { computed, onMounted, ref } from 'vue'
import ActionIcon from './ActionIcon.vue'
import HelpTip from './HelpTip.vue'
import TimePickerField from './TimePickerField.vue'
const props=defineProps({tool:String,api:Function})
const emit=defineEmits(['notice'])
const firstSetup=ref(false)
const discoveredAccessDirs=ref([])
const data=ref(null), revision=ref(''), busy=ref(false), failure=ref(''), chatEdit=ref(null), editIndex=ref(-1)
const sourceIds=computed(()=>data.value?.sync_sources?.map(s=>s.id)||[])
const titles={sync:'下载与简报设置',nas:'上传设置',wechat:'微信与 Codex 设置'}
async function load(){busy.value=true;try{const result=await props.api('/config?tool='+props.tool);data.value=result.data;discoveredAccessDirs.value=result.discovered_access_dirs||[];revision.value=result.revision;firstSetup.value=props.tool==='wechat'&&result.exists===false;failure.value=''}catch(e){failure.value=e.message}finally{busy.value=false}}
async function save(){busy.value=true;try{const result=await props.api('/config',{method:'POST',body:JSON.stringify({tool:props.tool,data:data.value,revision:revision.value})});data.value=result.data;revision.value=result.revision;firstSetup.value=false;failure.value='';emit('notice','配置已保存；运行中的工具需安全停止后重新启动。')}catch(e){failure.value=e.message;emit('notice',e.message,true)}finally{busy.value=false}}
function editChat(chat,index){editIndex.value=index;chatEdit.value=chat?JSON.parse(JSON.stringify(chat)):{id:'chat-'+Date.now(),name:'',username:'',kind:'group',reply:false,persona:'用简洁、准确的中文回复。',sync_sources:[]}}
function commitChat(){if(!chatEdit.value.id.trim()||!chatEdit.value.name.trim())return;if(editIndex.value<0)data.value.chats.push(chatEdit.value);else data.value.chats[editIndex.value]=chatEdit.value;chatEdit.value=null}
function adjustPrewarm(value){if(value===''||value==null)return;const ratio=Number(value);if(!Number.isFinite(ratio))return;if(ratio<=1){data.value.codex_session_prewarm=false;return}if(Number(data.value.codex_session_prewarm_percent)>=ratio)data.value.codex_session_prewarm_percent=Math.max(1,ratio-15)}
function addSource(){data.value.sync_sources??=[];data.value.sync_sources.push({id:'fusion'+(data.value.sync_sources.length||''),root:'../FusionLive同步工具',projects:['17580']})}
function removeSource(index){const id=data.value.sync_sources[index].id;if(data.value.chats.some(c=>c.sync_sources?.includes(id))){emit('notice','请先从会话授权中移除此来源。',true);return}data.value.sync_sources.splice(index,1)}
onMounted(load)
</script>
<template>
  <v-alert v-if="failure" type="error" variant="tonal" class="mb-5">{{failure}}</v-alert>
  <v-alert v-if="firstSetup" type="info" variant="tonal" class="mb-5">首次使用已载入默认数据库目录与账号，默认开启 Codex 回复及微信发送。未配置会话时可启动并保持待配置状态；配置会话后保存并重启监听。</v-alert>
  <v-progress-linear v-if="busy&&!data" indeterminate color="primary"/>
  <div v-if="data" class="settings-grid">
    <v-card class="settings-section">
      <h2 class="mb-1">{{titles[tool]}}</h2><p class="section-subtitle mb-6">直接使用工具现有配置文件，保存前校验内容。</p>
      <div v-if="tool==='sync'" class="settings-form">
        <div><h3 class="text-body-2 mb-2">后台解析</h3><p class="text-caption text-secondary">保存后安全停止并重新启动后台解析以应用。时间单位均为秒；空闲退出设为 0 表示持续等待。</p></div>
        <div class="settings-pair"><v-text-field v-model.number="data.analysis_poll_seconds" label="新文件扫描间隔" type="number" min="0.1" max="86400" step="1" suffix="秒"/><v-text-field v-model.number="data.analysis_idle_log_seconds" label="空闲状态提示间隔" type="number" min="0.1" max="86400" step="1" suffix="秒"/></div>
        <div class="settings-pair"><v-text-field v-model.number="data.analysis_idle_exit_seconds" label="手动启动空闲退出时间" type="number" min="0" max="86400" suffix="秒"/><v-text-field v-model.number="data.analysis_auto_idle_exit_seconds" label="自动启动空闲退出时间" type="number" min="0" max="86400" suffix="秒"/></div>
        <v-alert type="info" variant="tonal" density="compact">PCF 预制／安装分类使用设备连接坐标算法及 260818 预制率规则，保留命中规则编号；原 PCF 位置标记不再作为分类结果。</v-alert>
        <v-divider/>
        <v-text-field v-model="data.briefing_title" label="管道简报标题" maxlength="100"/>
        <div class="settings-pair"><TimePickerField v-model="data.prepare_time_beijing" label="简报生成时间（北京）" :disabled="busy"/><TimePickerField v-model="data.send_time_beijing" label="推送时间（北京）" :disabled="busy"/></div>
        <v-combobox v-model="data.recipients" label="推送会话" placeholder="输入完整会话名称后回车" multiple chips closable-chips/>
        <div class="switch-line"><div><strong>推送文字日报</strong><p>按已保存的会话名单发布每日下载情况。</p></div><v-switch v-model="data.publish_text" aria-label="推送文字日报"/></div>
        <div class="switch-line"><div><strong>推送简报图片</strong><p>发布主表截图，详细数据保留在 Excel 中。</p></div><v-switch v-model="data.publish_briefing" aria-label="推送简报图片"/></div>
        <div v-if="data.recipients.length" class="source-editor"><h3 class="text-body-2 mb-3">会话独立发布设置</h3><div v-for="recipient in data.recipients" :key="recipient" class="d-flex flex-wrap align-center ga-3 mb-3"><span class="flex-grow-1 text-body-2">{{recipient}}</span><v-checkbox :model-value="data.recipient_publications?.[recipient]?.publish_text??true" label="文字" hide-details density="compact" @update:model-value="value=>{data.recipient_publications??={};data.recipient_publications[recipient]??={};data.recipient_publications[recipient].publish_text=value}"/><v-checkbox :model-value="data.recipient_publications?.[recipient]?.publish_briefing??true" label="简报" hide-details density="compact" @update:model-value="value=>{data.recipient_publications??={};data.recipient_publications[recipient]??={};data.recipient_publications[recipient].publish_briefing=value}"/></div></div>
        <v-text-field v-model="data.test_recipient" label="手动推送测试对象"/><p class="text-caption text-secondary">下载时段为每天 02:00—10:30；PCF 预制／安装分类使用内置算法规则。</p>
      </div>
      <div v-else-if="tool==='nas'" class="settings-form">
        <v-text-field v-model="data.source_root" label="本地源目录" hint="相对于绿联云上传工具目录" persistent-hint/>
        <v-text-field v-model="data.download_ledger" label="同步工具下载台账"/>
        <v-text-field v-model="data.target_root" label="NAS 目标共享目录" placeholder="\\NAS\共享目录\阿联酋项目" hint="使用 SMB 共享路径。请先用运行管理中心的 Windows 用户在资源管理器登录该共享，并确认有写入权限。" persistent-hint/>
        <div class="settings-pair"><v-text-field v-model.number="data.retry_attempts" label="失败重试次数" type="number" min="1" max="5"/><v-select v-model="data.chunk_bytes" :items="[{title:'256 KiB',value:262144},{title:'1 MiB',value:1048576},{title:'4 MiB',value:4194304},{title:'8 MiB',value:8388608}]" label="传输分块大小"/></div>
        <div class="switch-line"><div><strong>保留历史版本</strong><p>保留原有上传策略中的历史版本。</p></div><v-switch v-model="data.preserve_previous_versions" aria-label="保留历史版本"/></div>
        <p class="text-caption text-secondary">上传时段固定为每天 12:00—次日 00:00。保存配置不会连接 NAS。</p>
      </div>
      <div v-else class="settings-form">
        <div class="switch-line"><div><strong>启用 Codex 回复</strong><p>仅为已启用回复的会话生成结果。</p></div><v-switch v-model="data.codex_enabled" aria-label="启用 Codex 回复"/></div>
        <div class="switch-line"><div><strong>允许发送微信</strong><p>关闭时只保留回复预览，不实际发送。</p></div><v-switch v-model="data.allow_send" aria-label="允许发送微信"/></div>
        <div class="switch-line"><div><strong>@ 回复人</strong><p>群聊排队、开始提示和最终回复在“回复对象”一行通过微信原生 @ 提醒提问人，默认开启；私聊不受影响。</p></div><v-switch v-model="data.mention_reply_sender" aria-label="@ 回复人"/></div>
        <div class="switch-line"><div><strong>查询进度提示</strong><p>Codex 开始执行查询工具时发送“正在查询……”，每条任务最多一次。</p></div><v-switch v-model="data.query_progress_enabled" aria-label="查询进度提示"/></div>
        <div class="switch-line"><div><strong>群聊摘要与问答记忆</strong><p>默认开启。已发送问答按会话生成 MD 摘要；下次仅带入相关记忆，动态数据仍核对当前台账。摘要与回复在同一次调用中生成。</p></div><v-switch v-model="data.chat_memory_enabled" aria-label="群聊摘要与问答记忆"/></div>
        <div class="settings-pair"><v-text-field v-model.number="data.reply_target_chars" label="优先回复长度（字符）" type="number" min="50" max="10000" hint="默认 600；详细问题可适当展开，仍受回复长度上限约束。" persistent-hint/><v-text-field v-model.number="data.recent_messages" label="带入最近消息条数" type="number" min="0" max="50" hint="默认 6 条；更早的相关问答由记忆补充。" persistent-hint/></div>
        <div class="settings-divider"/>
        <v-text-field v-model="data.database_root" label="微信数据库根目录"/>
        <div class="settings-pair"><v-text-field v-model="data.database_account" label="数据库账号目录"/><v-text-field v-model="data.account_name" label="微信显示名称"/></div>
        <v-combobox v-model="data.codex_command" label="Codex 启动参数" multiple chips hint="每项一个参数，保留列表顺序" persistent-hint/>
        <v-select v-model="data.codex_backend" label="微信 Codex 后端" aria-label="微信 Codex 后端" :items="[{title:'常驻 App Server（默认）',value:'app-server'},{title:'逐次 CLI（兼容模式）',value:'exec'}]" hint="常驻模式为每个群或私聊复用独立会话，达到设置的上下文比例后轮换；保存后重启微信监听。" persistent-hint/>
        <template v-if="data.codex_backend==='app-server'">
          <v-text-field v-model.number="data.codex_session_rotate_percent" label="上下文轮换比例（%）" type="number" min="1" max="99" hint="默认 70%；达到比例后完成本轮，下一条问题切换到新会话。" persistent-hint @blur="adjustPrewarm(data.codex_session_rotate_percent)"/>
          <div class="switch-line"><div><strong>提前创建下一会话</strong><p>默认在 55% 时后台准备空会话，切换时带入最新群聊摘要；不提前发送问题。</p></div><v-switch v-model="data.codex_session_prewarm" aria-label="提前创建下一会话"/></div>
          <v-text-field v-model.number="data.codex_session_prewarm_percent" label="提前创建比例（%）" type="number" min="1" max="98" :disabled="!data.codex_session_prewarm" hint="必须小于轮换比例；创建未完成时，切换仍可能需要等待。" persistent-hint/>
          <div class="settings-pair"><v-text-field v-model.number="data.codex_context_window_tokens" label="上下文窗口 Token 数（0 为自动）" type="number" min="0" max="10000000" hint="仅用于比例计算，不改变模型窗口；0 采用 Codex 报告值，未报告时可按实际模型填写。" persistent-hint/><v-text-field v-model.number="data.codex_session_unknown_max_turns" label="用量未知时最多复用轮数" type="number" min="2" max="500" hint="仅在缺少上下文用量或窗口数据时使用，默认 20 轮。" persistent-hint/></div>
        </template>
        <div class="settings-pair"><v-text-field v-model="data.codex_model" label="Codex 模型（留空使用默认）"/><v-text-field v-model.number="data.codex_timeout_seconds" label="单次任务超时（秒）" type="number" min="5" max="1800"/></div>
        <v-text-field v-model.number="data.codex_idle_timeout_seconds" label="无进度超时（秒，0 为关闭）" type="number" min="0" max="1800" hint="默认 300 秒；正在执行查询工具时不触发无进度超时，总时限仍然有效。" persistent-hint/>
        <v-text-field v-model.number="data.reply_max_age_seconds" label="消息回复有效期（秒）" type="number" min="10" max="3600" hint="排队不计时；任务开始后计算生成及发送重试时间。接收时仍会过滤过旧消息。" persistent-hint/>
        <div class="settings-pair"><v-text-field v-model.number="data.send_max_attempts" label="发送最多尝试次数（含首次）" type="number" min="1" max="10"/><v-text-field v-model.number="data.send_retry_seconds" label="发送重试间隔（秒）" type="number" min="1" max="300"/></div>
        <p class="text-caption text-secondary">确认未发送的最终回复会重试；发送结果不确定时核对聊天记录，不重复提交。</p>
        <v-text-field v-model.number="data.file_send_cleanup_days" label="发送暂存文件清理间隔" aria-label="发送暂存文件清理间隔" type="number" min="0" max="365" suffix="天" hint="默认每 2 天清理一次，同时只清理保留满该天数的 ZIP 和发送副本；0 为关闭。待发送、待续发和结果不明的文件保留，下载原文件不受影响。保存后重启微信监听生效。" persistent-hint/>
        <v-select v-model="data.codex_transport" label="共享 Codex 连接方式" aria-label="共享 Codex 连接方式" :items="[{title:'跟随 Codex 默认',value:'auto'},{title:'尝试 WebSocket',value:'websocket'},{title:'HTTPS 流式（跳过 WebSocket）',value:'https'}]" hint="微信群聊使用此设置；网页会话选择继承共享设置时也使用此设置。" persistent-hint/>
        <div class="switch-line"><div><strong>自动包含同级目录</strong><p>自动发现 codex-wechat 所在位置的同级目录；关闭后可手动增删。</p></div><v-switch v-model="data.codex_auto_access_dirs" aria-label="自动包含同级目录" @update:model-value="value=>{if(value)data.codex_access_dirs=[...discoveredAccessDirs]}"/></div>
        <v-combobox v-model="data.codex_access_dirs" label="Codex 可访问目录" aria-label="Codex 可访问目录" multiple chips closable-chips :disabled="data.codex_auto_access_dirs" hint="每项一个完整路径，输入后回车；相对路径以 codex-wechat 目录为基准。" persistent-hint/>
        <p class="text-caption text-secondary">网页对话和微信群聊使用同一目录列表；微信群聊只读。保存目录不会修改 Windows 文件夹本身的权限。微信监听需重启应用新配置。</p>
        <div class="section-toolbar"><h2>监听会话</h2><v-btn variant="tonal" prepend-icon="mdi-plus" @click="editChat(null,-1)">添加会话</v-btn></div>
        <v-table v-if="data.chats.length"><thead><tr><th>会话名称</th><th>类型 / 回复</th><th>项目文件来源</th><th>操作</th></tr></thead><tbody><tr v-for="(chat,index) in data.chats" :key="index"><td><strong>{{chat.name}}</strong><p class="text-caption text-secondary">{{chat.username||'未填写微信原生 ID'}}</p></td><td>{{chat.kind==='group'?'群聊':'私聊'}} · {{chat.reply?'回复开启':'仅记录'}}</td><td>{{chat.sync_sources?.join('、')||'使用共享目录'}}</td><td><ActionIcon :label="'编辑 '+chat.name" icon="mdi-pencil-outline" @click="editChat(chat,index)"/></td></tr></tbody></v-table><p v-else class="text-body-2 text-secondary">尚未配置会话。</p>
        <div class="section-toolbar"><div class="d-flex align-center ga-2"><h2>项目文件来源</h2><HelpTip text="独立来源可细分台账项目；会话未选择来源时，从共享可访问目录自动发现同步台账。"/></div><v-btn variant="tonal" prepend-icon="mdi-plus" @click="addSource">添加来源</v-btn></div>
        <div><div v-for="(source,index) in data.sync_sources" :key="index" class="source-editor"><div class="settings-pair mb-4"><v-text-field v-model="source.id" label="来源 ID"/><v-text-field v-model="source.root" label="同步工具目录"/></div><v-combobox v-model="source.projects" label="允许访问的项目 ID" multiple chips closable-chips/><v-btn variant="text" color="error" class="mt-2" @click="removeSource(index)">移除此来源</v-btn></div><p v-if="!data.sync_sources?.length" class="text-body-2 text-secondary">未配置独立来源，将从共享可访问目录自动发现同步台账。</p></div>
      </div>
      <div class="save-bar"><p>保存后重启对应工具以应用配置。</p><v-btn color="primary" prepend-icon="mdi-content-save-outline" :loading="busy" @click="save">保存配置</v-btn></div>
    </v-card>
    <v-card class="pa-5 settings-aside"><v-icon icon="mdi-tune-variant" color="primary" size="30" class="mb-4"/><h3>与现有工具保持一致</h3><p>此页面读写各工具原有配置，不另建一份运行配置。</p><p v-if="tool==='sync'">简报生成和微信推送相互独立。文件数为已发现文件，解析指标为有效数据小计。</p><p v-else-if="tool==='nas'">上传仍使用 Windows 当前 SMB 会话或原有环境认证，不在页面保存 NAS 密码。</p><p v-else>群聊需要 @ 当前账号才触发回复。可访问目录与网页对话共享；独立来源可细分台账项目，未知数据不冒充解析结果。</p><p>配置校验失败时会保留原配置。</p></v-card>
  </div>
  <v-dialog :model-value="!!chatEdit" @update:model-value="chatEdit=null" max-width="640"><v-card v-if="chatEdit" class="pa-6"><h2 class="mb-5">{{editIndex<0?'添加会话':'编辑会话'}}</h2><div class="settings-form"><div class="settings-pair"><v-text-field v-model="chatEdit.id" label="本地会话 ID"/><v-select v-model="chatEdit.kind" :items="[{title:'群聊',value:'group'},{title:'私聊',value:'private'}]" label="类型"/></div><v-text-field v-model="chatEdit.name" label="会话完整名称"/><v-text-field v-model="chatEdit.username" label="微信原生账号 / 群 ID"/><v-textarea v-model="chatEdit.persona" label="回复要求" rows="3"/><v-select v-model="chatEdit.sync_sources" :items="sourceIds" label="独立台账来源（留空使用共享目录）" multiple chips/><v-switch v-model="chatEdit.reply" label="为此会话启用回复"/></div><v-card-actions class="mt-5"><v-btn v-if="editIndex>=0" color="error" @click="data.chats.splice(editIndex,1);chatEdit=null">移除会话</v-btn><v-spacer/><v-btn @click="chatEdit=null">取消</v-btn><v-btn color="primary" variant="flat" :disabled="!chatEdit.id.trim()||!chatEdit.name.trim()" @click="commitChat">保留修改</v-btn></v-card-actions></v-card></v-dialog>
</template>
