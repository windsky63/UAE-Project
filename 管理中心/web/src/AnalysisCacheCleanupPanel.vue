<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
const props=defineProps({api:Function})
const emit=defineEmits(['notice'])
const task=ref(null), busy=ref(false), failure=ref('')
const active=computed(()=>busy.value||task.value?.state==='running')
const names={running:'清理中',succeeded:'已完成',failed:'执行失败',detached:'待核对'}
let timer=null, alive=true, sequence=0, submitted=''
async function refresh(){
  const version=++sequence
  try{
    const result=await props.api('/tasks?tool=sync&limit=100')
    if(!alive||version!==sequence)return
    const latest=result.items.find(row=>row.action==='clean-analysis-cache'&&(!submitted||row.id===submitted))
    if(latest)task.value=latest
    failure.value=''
  }catch(e){if(alive&&version===sequence)failure.value=e.message}
}
async function start(){
  busy.value=true;failure.value=''
  try{
    const result=await props.api('/action',{method:'POST',body:JSON.stringify({tool:'sync',action:'clean-analysis-cache'})})
    submitted=result.id
    task.value={...result,action:'clean-analysis-cache',state:'running'}
    emit('notice','旧解析缓存校验已提交，可在任务记录或运行日志查看清理详情。')
    await refresh()
  }catch(e){failure.value=e.message;emit('notice',e.message,true)}finally{busy.value=false}
}
onMounted(()=>{refresh();timer=setInterval(()=>{if(active.value)refresh()},1000)})
onUnmounted(()=>{alive=false;sequence++;clearInterval(timer)})
</script>

<template>
  <v-card class="mb-6 pa-5" aria-label="旧解析缓存清理">
    <div class="d-flex align-center justify-space-between flex-wrap ga-3 mb-3">
      <h2>解析缓存清理</h2>
      <v-chip v-if="task" size="small" :color="task.state==='failed'?'error':active?'primary':'secondary'">{{names[task.state]||task.state}}</v-chip>
    </div>
    <p class="section-subtitle mb-4">手动执行一次解析工作流中的旧任务校验。确认新版解析成功后，清理旧解压内容和不再使用的共享缓存；原始压缩包、解析报告及版本记录保留。</p>
    <div class="d-flex flex-wrap ga-3 mb-3">
      <v-btn color="primary" prepend-icon="mdi-broom" :loading="active" :disabled="active" @click="start">清理旧解析缓存</v-btn>
      <v-btn variant="text" :disabled="busy" @click="refresh">刷新缓存清理状态</v-btn>
    </div>
    <p class="text-caption text-secondary">执行前请安全停止下载、后台解析和文件投递。新版未完成、关联不明确或结果无法核验时保留旧缓存。</p>
    <v-progress-linear v-if="active" indeterminate color="primary" class="mt-3"/>
    <p v-if="task" class="text-body-2 mt-3" aria-live="polite">{{active?'正在核对旧解析任务并清理缓存…':task.state==='succeeded'?'校验清理已完成；清理数量及保留原因请查看本次任务日志。':task.state==='failed'?'本次清理未完成，请查看任务日志后重试。':'任务状态待核对，请查看任务日志。'}}</p>
    <v-alert v-if="failure" type="error" variant="tonal" class="mt-3">{{failure}}</v-alert>
  </v-card>
</template>
