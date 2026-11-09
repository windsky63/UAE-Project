<script setup>
import {computed,onMounted,onUnmounted,ref} from 'vue'
const props=defineProps({settings:Object,method:String,api:Function,saveConfig:Function,reloadConfig:Function})
const emit=defineEmits(['notice','method'])
const ssh=computed(()=>props.method==='ssh')
const status=ref(null),busy=ref(false),failure=ref('')
let timer
const tone=computed(()=>status.value?.state==='ready'?'success':status.value?.state==='failed'?'error':'info')
const gb=value=>(Number(value||0)/1e9).toFixed(3)
async function refresh(){
  try{
    status.value=await props.api('/nas/hash-status');failure.value=''
    clearTimeout(timer)
    if(status.value.state==='checking')timer=setTimeout(refresh,1500)
  }catch(e){failure.value=e.message}
}
async function act(kind){
  busy.value=true;failure.value=''
  try{
    if(!await props.saveConfig())return
    const result=await props.api('/nas/'+kind,{method:'POST',body:JSON.stringify({})})
    if(kind==='setup')emit('notice',result.message)
    else emit('notice','连接检查已启动；验证过程不修改业务文件。')
    await refresh()
  }catch(e){failure.value=e.message;emit('notice',e.message,true)}finally{busy.value=false}
}
async function reload(){await props.reloadConfig();await refresh()}
onMounted(refresh)
onUnmounted(()=>clearTimeout(timer))
</script>
<template>
  <section class="settings-form" aria-label="NAS本机校验设置">
    <v-divider/>
    <div><h3 class="text-body-1 mb-2">文件校验方式</h3><p class="text-caption text-secondary">同一轮复用未变化文件的哈希；内容变化后重新计算。</p></div>
    <v-select :model-value="method" @update:model-value="value=>emit('method',value)" :items="[{title:'SSH 本机校验（默认）',value:'ssh'},{title:'SMB 完整回读校验',value:'smb'}]" label="文件校验方式"/>
    <p v-if="ssh" class="text-caption text-secondary">NAS 在本机计算 SHA-256，仅返回结果。SSH 失败会报告错误并保留进度，不会回退到 SMB 校验。</p>
    <p v-else class="text-caption text-secondary">通过 SMB 完整读取 NAS 文件并计算 SHA-256，会占用服务器与 NAS 之间的网络带宽。</p>
    <v-alert v-if="status" :type="tone" variant="tonal" density="compact">已保存配置：{{status.message}}</v-alert>
    <v-alert v-if="failure" type="error" variant="tonal">{{failure}}</v-alert>
    <template v-if="ssh">
    <div class="settings-pair"><v-text-field v-model="settings.host" label="SSH 地址" autocomplete="off"/><v-text-field v-model.number="settings.port" label="SSH 端口" type="number" min="1" max="65535"/></div>
    <v-text-field v-model="settings.username" label="SSH 用户名" autocomplete="off"/>
    <p class="text-caption text-secondary">{{settings.root_path?'NAS 目录已配置：'+settings.root_path:'NAS 目录会在首次配置时自动识别，无需在这里手动填写。'}}</p>
    <v-expansion-panels variant="accordion"><v-expansion-panel title="密钥与连接选项"><v-expansion-panel-text><div class="settings-form">
      <v-text-field v-model="settings.root_path" label="NAS 内部目录（自动识别）" hint="向导按已有SMB目标自动识别并验证。仅自动识别失败时需要手动修正。" persistent-hint/>
      <v-text-field v-model="settings.identity_file" label="服务器私钥路径" hint="只填写运行上传工具的服务器上的文件路径。" persistent-hint/>
      <v-text-field v-model="settings.known_hosts_file" label="服务器主机指纹文件"/>
      <div class="settings-pair"><v-text-field v-model="settings.python_command" label="NAS Python 命令"/><v-text-field v-model.number="settings.timeout_seconds" label="单文件校验超时" type="number" min="1" max="86400" suffix="秒"/></div>
    </div></v-expansion-panel-text></v-expansion-panel></v-expansion-panels>
    <div class="d-flex flex-wrap ga-3"><v-btn variant="tonal" :loading="busy" @click="act('setup')">首次配置向导</v-btn><v-btn color="primary" variant="tonal" :disabled="!settings.root_path||busy||status?.state==='checking'" @click="act('hash-check')">保存并检查连接</v-btn><v-btn variant="text" :disabled="busy" @click="reload">刷新配置与状态</v-btn></div>
    <p class="text-caption text-secondary">首次配置向导会在服务器桌面打开终端，请在那里输入用户名和密码。完成后刷新此页；密码不会经过网页或写入配置。运行中的上传工具需安全重启。</p>
    </template>
    <div v-if="status?.last_run" class="source-editor"><h4 class="text-body-2 mb-2">最近一轮实际校验</h4><p class="text-caption text-secondary">{{status.last_run.started}} · {{status.last_run.status}}</p><p v-if="status.last_run.remote_hash_files!=null" class="text-body-2">NAS 本机计算 {{status.last_run.remote_hash_files}} 次 / {{gb(status.last_run.remote_hash_bytes)}} GB；SMB 回读 {{gb(status.last_run.nas_read_bytes)}} GB；同轮复用 {{status.last_run.hash_cache_hits||0}} 次。{{status.last_run.remote_hash_fell_back?'该轮曾回退到 SMB。':''}}</p><p v-else class="text-body-2">该轮尚未记录 NAS 本机校验结果。</p></div>
  </section>
</template>
