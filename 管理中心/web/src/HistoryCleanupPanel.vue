<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
const props=defineProps({api:Function})
const emit=defineEmits(['notice'])
const status=ref({state:'idle',manual_only:true})
const busy=ref(false), submitted=ref(''), failure=ref('')
let timer=null, alive=true, version=0
const active=computed(()=>!!submitted.value||status.value.state==='running')
const names={idle:'尚未执行',running:'清理中',succeeded:'已完成',stopped:'已停止',failed:'执行失败',interrupted:'已中断'}
function number(value){return Number(value||0).toLocaleString('zh-CN')}
function space(value){const n=Number(value||0);return n>=1073741824?(n/1073741824).toFixed(2)+' GB':(n/1048576).toFixed(1)+' MB'}
async function refresh(){
  const requested=++version
  try{
    const result=await props.api('/history-cleanup')
    if(!alive||requested!==version)return
    if(submitted.value&&result.job_id!==submitted.value)return
    status.value=result;submitted.value='';failure.value=''
  }catch(e){if(alive)failure.value=e.message}
}
async function start(){
  busy.value=true;failure.value=''
  try{
    const task=await props.api('/action',{method:'POST',body:JSON.stringify({tool:'sync',action:'clean-history'})})
    submitted.value=task.id
    emit('notice','历史清理已提交，仅本次手动执行。')
    await refresh()
  }catch(e){failure.value=e.message;emit('notice',e.message,true)}finally{busy.value=false}
}
async function stop(){
  busy.value=true
  try{
    await props.api('/action',{method:'POST',body:JSON.stringify({tool:'sync',action:'stop-history-cleanup'})})
    emit('notice','已请求安全停止，当前文件处理完成后退出。')
    await refresh()
  }catch(e){failure.value=e.message}finally{busy.value=false}
}
onMounted(()=>{refresh();timer=setInterval(()=>{if(active.value)refresh()},1000)})
onUnmounted(()=>{alive=false;clearInterval(timer)})
</script>

<template>
  <v-card class="mb-6 pa-5 history-cleanup-panel" aria-label="历史文件手动清理">
    <div class="d-flex align-center justify-space-between flex-wrap ga-3 mb-3">
      <h2>历史文件清理</h2><v-chip size="small" :color="status.state==='failed'?'error':active?'primary':'secondary'">{{submitted?'已提交':names[status.state]||status.state}}</v-chip>
    </div>
    <p class="section-subtitle mb-4">仅手动执行，不随下载或定时任务自动清理。保留初次下载内容和最近两次旧内容；仍被引用或无法核验的文件保留。</p>
    <div class="d-flex flex-wrap ga-3 mb-4">
      <v-btn color="primary" prepend-icon="mdi-broom" :loading="busy&&!active" :disabled="busy||active" @click="start">清理历史文件</v-btn>
      <v-btn v-if="active" variant="outlined" prepend-icon="mdi-stop-circle-outline" :loading="busy&&active" :disabled="busy||!!submitted" @click="stop">停止清理</v-btn>
      <v-btn variant="text" :disabled="busy" @click="refresh">刷新清理进度</v-btn>
    </div>
    <p class="text-caption text-secondary mb-3">执行前请安全停止下载、后台解析和文件投递；清理前自动备份台账。</p>
    <v-alert v-if="failure" type="error" variant="tonal" class="mb-3">{{failure}}</v-alert>
    <div v-if="status.job_id||submitted" aria-live="polite">
      <v-progress-linear v-if="active" indeterminate color="primary" class="mb-3"/>
      <p v-if="active" class="mb-2">{{submitted?'正在启动后台任务…':status.phase||'正在核验文件'}}</p>
      <div class="d-flex flex-wrap ga-5 text-body-2">
        <span>登记候选 <strong>{{number(status.planned_files)}}</strong></span>
        <span>实际删除 <strong>{{number(status.deleted_files)}}</strong></span>
        <span>释放空间 <strong>{{space(status.deleted_bytes)}}</strong></span>
        <span>跳过 <strong>{{number(status.skipped_files)}}</strong></span>
      </div>
      <p v-if="active&&status.current_file" class="text-caption text-secondary mt-3 cleanup-current-file">{{status.current_file}}</p>
      <p v-if="status.message" class="text-body-2 mt-3">{{status.message}}</p>
    </div>
  </v-card>
</template>

<style scoped>
.cleanup-current-file { overflow-wrap:anywhere; }
</style>
