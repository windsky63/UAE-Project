<script setup>
import {ref,computed,onMounted,onUnmounted,watch} from 'vue'
import TimePickerField from './TimePickerField.vue'
import ValueListField from './ValueListField.vue'
const props=defineProps({api:Function,compact:Boolean,date:String,refreshToken:Number}),emit=defineEmits(['notice','deliveries'])
defineOptions({inheritAttrs:false})
const data=ref(null),saved=ref(''),revision=ref(''),report=ref({}),deliveries=ref([]),today=ref(''),busy=ref(false),failure=ref(''),task=ref(''),confirmation=ref(false)
let timer,alive=true,request=0
const dirty=computed(()=>data.value&&JSON.stringify(data.value)!==saved.value)
const canSend=computed(()=>report.value.ready&&report.value.date===today.value&&data.value?.recipients?.length&&!dirty.value&&!busy.value&&!task.value)
const statusText=computed(()=>{const rows=deliveries.value.filter(r=>r.day===report.value.date);if(rows.some(r=>['uncertain','sending'].includes(r.state)))return '待核对';if(rows.some(r=>r.state==='not_sent'))return '发送失败';if(data.value?.recipients?.length&&data.value.recipients.every(name=>rows.some(r=>r.recipient===name&&r.state==='submitted')))return '已提交微信';return '未发送'})
async function load(){const current=++request;try{const r=await props.api('/config?tool=unit_text'+(props.date?'&date='+encodeURIComponent(props.date):''));if(!alive||current!==request)return;data.value=r.data;saved.value=JSON.stringify(r.data);revision.value=r.revision;report.value=r.snapshot||{};deliveries.value=r.deliveries||[];emit('deliveries',deliveries.value);today.value=r.today||r.snapshot?.date||'';failure.value=''}catch(e){if(current===request)failure.value=e.message}}
async function save(){busy.value=true;try{const r=await props.api('/config',{method:'POST',body:JSON.stringify({tool:'unit_text',data:data.value,revision:revision.value})});data.value=r.data;saved.value=JSON.stringify(r.data);revision.value=r.revision;emit('notice','单元资料推送文设置已保存');failure.value='';return true}catch(e){failure.value=e.message;return false}finally{busy.value=false}}
async function poll(){if(!alive||!task.value)return;try{const r=await props.api('/tasks?tool=analysis&limit=100');const row=r.items.find(v=>v.id===task.value);if(row&&row.state!=='running'){task.value='';await load();if(row.state!=='succeeded')failure.value='单元资料推送文任务未完成，请查看解析工具操作记录和日志。';return}}catch(e){failure.value=e.message}if(alive)timer=setTimeout(poll,1500)}
async function run(action,extra={}){if(task.value||busy.value)return;if(dirty.value&&!await save())return;busy.value=true;try{const r=await props.api('/action',{method:'POST',body:JSON.stringify({tool:'analysis',action,...extra})});task.value=r.id;confirmation.value=false;failure.value='';timer=setTimeout(poll,500);emit('notice',action==='unit-text'?'单元资料推送文生成已提交':'单元资料推送文发送已提交')}catch(e){failure.value=e.message}finally{busy.value=false}}
onMounted(load);onUnmounted(()=>{alive=false;clearTimeout(timer)})
watch(()=>[props.date,props.refreshToken],load)
</script>
<template>
 <v-card v-bind="$attrs" class="unit-text-panel" :class="compact?'pa-5 h-100':'settings-section'">
  <div class="d-flex align-center flex-wrap ga-3"><h2 class="flex-grow-1">单元资料推送文</h2><v-chip v-if="compact" :color="statusText==='已提交微信'?'success':statusText==='待核对'?'warning':statusText==='发送失败'?'error':'secondary'" size="small">{{statusText}}</v-chip></div>
  <p v-if="compact" class="text-caption text-secondary mt-3">快照时间：{{report.generated_at?new Date(report.generated_at).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'—'}} · {{report.ready?'已就绪':'尚未就绪'}}；发送使用这份快照并留存备份。</p>
  <p v-else class="section-subtitle mt-2 mb-5">独立设置微信接收会话和发送时间，随资料解析调度运行；手动生成请到运行管理，预览和发送请到每日推送。</p>
  <v-alert v-if="failure" type="error" variant="tonal" class="mb-4">{{failure}}</v-alert>
  <div v-if="data" :class="{'settings-form':!compact}">
   <div v-if="!compact" class="settings-pair"><v-switch v-model="data.enabled" label="定时发送单元资料推送文" :disabled="busy||!!task"/><TimePickerField v-model="data.send_time_beijing" label="单元资料发送时间（北京）" :disabled="busy||!!task"/></div>
   <ValueListField v-if="!compact" v-model="data.recipients" label="单元资料接收会话" hint="此名单独立于原下载日报和图纸文件接收群。" :disabled="busy||!!task"/>
   <v-btn v-if="!compact" variant="outlined" :loading="busy" :disabled="!!task" @click="save">保存单元资料设置</v-btn>
   <template v-if="compact">
    <v-alert v-if="report.reason" type="info" variant="tonal" class="my-3">{{report.reason}}</v-alert>
    <pre v-if="report.text" class="unit-text-body mt-4">{{report.text}}</pre>
    <div class="d-flex flex-wrap ga-3 mt-4"><v-btn variant="outlined" :disabled="!canSend" @click="confirmation=true">发送单元资料推送文</v-btn></div>
   </template>
  </div>
 </v-card>
 <v-dialog v-model="confirmation" max-width="760" :persistent="busy"><v-card title="确认发送单元资料推送文"><v-card-text><p class="mb-4">接收会话：{{data?.recipients.join('、')}}</p><pre class="unit-text-body">{{report.text}}</pre></v-card-text><v-card-actions><v-spacer/><v-btn @click="confirmation=false">取消</v-btn><v-btn color="primary" :disabled="!canSend" :loading="busy" @click="run('unit-text-send',{confirmed:true,expected_sha:report.sha256})">确认发送</v-btn></v-card-actions></v-card></v-dialog>
</template>
<style scoped>.unit-text-body{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.8;max-height:600px;overflow:auto}</style>
