<script setup>
import {onUnmounted,ref} from 'vue'
const props=defineProps({api:Function})
const emit=defineEmits(['notice'])
const exporting=ref(false),taskId=ref(''),message=ref(''),failed=ref(false)
let timer,alive=true
async function check(){
  try{
    const result=await props.api('/tasks?tool=sync&limit=100')
    if(!alive)return
    const task=result.items.find(item=>item.id===taskId.value)
    if(!task||task.state==='running'){timer=setTimeout(check,1500);return}
    exporting.value=false
    failed.value=task.state!=='succeeded'
    message.value=failed.value?'文件台账导出未完成，请在下方操作记录中查看日志。':'文件台账已在原导出目录重新生成。若原文件被 Excel 占用，结果保存为同目录的“最新”或带时间戳的文件。'
    emit('notice',message.value,failed.value)
  }catch(e){
    if(!alive)return
    exporting.value=false;failed.value=true
    message.value='无法获取导出结果：'+e.message+'。任务可能仍在运行，请在下方操作记录中查看。'
  }
}
async function start(){
  if(exporting.value)return
  exporting.value=true;failed.value=false;message.value='正在原导出目录重新生成文件台账…'
  try{
    const task=await props.api('/action',{method:'POST',body:JSON.stringify({tool:'sync',action:'export-ledger'})})
    if(!alive)return
    taskId.value=task.id
    await check()
  }catch(e){if(alive){exporting.value=false;failed.value=true;message.value=e.message;emit('notice',e.message,true)}}
}
onUnmounted(()=>{alive=false;clearTimeout(timer)})
</script>

<template>
  <div class="maintenance-action">
    <v-btn variant="outlined" prepend-icon="mdi-file-excel-outline" :loading="exporting" :disabled="exporting" @click="start">导出文件台账</v-btn>
    <Teleport defer to="#tool-task-progress">
      <div v-if="message" aria-label="文件台账导出" class="mt-4" aria-live="polite">
        <v-progress-linear v-if="exporting" indeterminate color="primary" class="mb-2"/>
        <v-alert :type="failed?'error':exporting?'info':'success'" variant="tonal">{{message}}</v-alert>
      </div>
    </Teleport>
  </div>
</template>
<style scoped>.maintenance-action{display:contents}</style>
