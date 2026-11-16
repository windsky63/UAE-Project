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
    const result=await props.api('/tasks?tool=analysis&limit=100')
    if(!alive||version!==sequence)return
    const latest=result.items.find(row=>row.action==='clean-analysis-cache'&&(!submitted||row.id===submitted))
    if(latest)task.value=latest
    failure.value=''
  }catch(e){if(alive&&version===sequence)failure.value=e.message}
}
async function start(){
  busy.value=true;failure.value=''
  try{
    const result=await props.api('/action',{method:'POST',body:JSON.stringify({tool:'analysis',action:'clean-analysis-cache'})})
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
  <div class="maintenance-action">
    <v-btn variant="outlined" prepend-icon="mdi-broom" :loading="active" :disabled="active" @click="start">清理旧解析缓存</v-btn>
    <Teleport defer to="#tool-task-progress">
      <div v-if="task||failure||active" class="mt-4" aria-label="旧解析缓存清理" aria-live="polite">
        <div class="d-flex align-center ga-3 mb-2"><strong>解析缓存清理</strong><v-chip v-if="task" size="small" :color="task.state==='failed'?'error':active?'primary':'secondary'">{{names[task.state]||task.state}}</v-chip></div>
        <v-progress-linear v-if="active" indeterminate color="primary" class="mb-2"/>
        <p v-if="task">{{active?'正在核对旧解析任务并清理缓存…':task.state==='succeeded'?'校验清理已完成；清理数量及保留原因请查看本次任务日志。':task.state==='failed'?'本次清理未完成，请查看任务日志后重试。':'任务状态待核对，请查看任务日志。'}}</p>
        <v-alert v-if="failure" type="error" variant="tonal" class="mt-3">{{failure}}</v-alert>
      </div>
    </Teleport>
  </div>
</template>
<style scoped>.maintenance-action{display:contents}</style>
