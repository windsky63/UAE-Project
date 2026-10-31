<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
const props=defineProps({api:{type:Function,required:true}})
const emit=defineEmits(['notice'])
const data=ref(null), day=ref(''), loading=ref(false), failure=ref('')
const dialog=ref(false), sending=ref(false), sendError=ref(''), kind=ref('all'), task=ref('')
const generating=ref(false), generationTask=ref('')
let timer, request=0
const names={pending:'未发送',submitted:'已提交微信',not_sent:'发送失败／未发送',uncertain:'发送结果待核对',unknown:'发送状态读取失败',previous_or_partial:'已提交部分内容或旧版本'}
const phases={waiting:'等待准备',preparing:'正在生成',waiting_analysis:'等待分析',prepared:'已准备',sending:'正在推送',submitted:'已提交微信',uncertain:'待人工核对',skipped:'已跳过',disabled:'自动发布已关闭',not_sent:'未发送'}
const receivers=computed(()=>(data.value?.targets||[]).map(t=>({...t,text:t.text&&kind.value!=='briefing',briefing:t.briefing&&kind.value!=='text'})).filter(t=>t.text||t.briefing))
const canSend=computed(()=>data.value&&data.value.date===data.value.today&&receivers.value.length>0
  &&(!receivers.value.some(t=>t.text)||data.value.text.ready)
  &&(!receivers.value.some(t=>t.briefing)||data.value.briefing.ready))
function stamp(value){return value?new Date(value).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'—'}
function color(state){return state==='submitted'?'success':state==='uncertain'?'warning':state==='not_sent'?'error':'secondary'}
async function load(){
  const current=++request
  loading.value=true
  try{
    const result=await props.api('/publications'+(day.value?'?date='+encodeURIComponent(day.value):''))
    if(current===request){data.value=result;failure.value=''}
  }catch(error){if(current===request)failure.value=error.message}
  finally{if(current===request)loading.value=false}
}
function openSend(value){kind.value=value;sendError.value='';dialog.value=true}
async function generateText(){
  if(generating.value||!data.value||data.value.date!==data.value.today)return
  generating.value=true
  try{
    const result=await props.api('/action',{method:'POST',body:JSON.stringify({tool:'sync',action:'generate-text'})})
    generationTask.value=result.id
    emit('notice','生成推送文任务已提交，内容会自动刷新；也可在同步工具操作记录中查看结果。')
    await load()
  }catch(error){emit('notice',error.message,true)}
  finally{generating.value=false}
}
async function send(){
  if(sending.value||!canSend.value)return
  sending.value=true;sendError.value=''
  try{
    const result=await props.api('/publications/push',{method:'POST',body:JSON.stringify({date:data.value.date,kind:kind.value,confirmed:true})})
    task.value=result.id;dialog.value=false
    emit('notice','手动推送任务已提交，实际结果会在本页更新，也可查看同步工具操作记录。')
    await load()
  }catch(error){sendError.value=error.message}
  finally{sending.value=false}
}
watch(day,load)
onMounted(()=>{load();timer=setInterval(()=>{if(!document.hidden&&!loading.value&&!dialog.value)load()},5000)})
onUnmounted(()=>{request++;clearInterval(timer)})
</script>

<template>
  <div data-testid="publications-panel">
    <v-card class="mb-5 pa-5">
      <div class="d-flex align-center flex-wrap ga-3">
        <div class="flex-grow-1"><h2>每日推送与管道简报</h2><p class="section-subtitle">按北京时间查看文件与实际投递记录，每5秒刷新状态。</p></div>
        <v-text-field v-model="day" type="date" :max="data?.today" label="查看日期（留空为今天）" hide-details style="max-width:250px" :disabled="sending||dialog"/>
        <v-btn variant="text" :loading="loading" @click="load">刷新</v-btn>
        <v-btn color="primary" prepend-icon="mdi-send-outline" :disabled="!data||data.date!==data.today||sending||!!failure" @click="openSend('all')">手动推送</v-btn>
      </div>
      <v-alert v-if="failure" type="error" variant="tonal" class="mt-4">{{failure}}</v-alert>
      <template v-if="data">
        <p class="text-body-2 mt-4">所选日期：{{data.date}} · 自动调度：{{phases[data.schedule.phase]||data.schedule.phase||'暂无记录'}}<span v-if="data.schedule.reason">；{{data.schedule.reason}}</span></p>
        <p class="text-caption text-secondary mt-2">“已提交微信”表示客户端发送动作已完成，不代表接收人已收到。结果不确定的记录需在微信中核对。</p>
        <v-alert v-for="error in data.errors" :key="error" type="warning" variant="tonal" class="mt-3">{{error}}</v-alert>
        <v-alert v-if="task" type="info" variant="tonal" class="mt-3">手动推送任务编号：{{task}}。请等待下方投递状态更新。</v-alert>
        <p v-if="data.manual" class="text-body-2 mt-3">最近手动推送：{{phases[data.manual.phase]||data.manual.phase}} · 开始 {{stamp(data.manual.started_at)}} · 结束 {{stamp(data.manual.ended_at)}}<span v-if="data.manual.error">；{{data.manual.error}}</span><span v-if="data.manual.result?.errors?.length">；{{data.manual.result.errors.join('；')}}</span></p>
      </template>
    </v-card>
    <template v-if="data">
      <v-row>
        <v-col cols="12" lg="6">
          <v-card class="pa-5 h-100">
            <div class="d-flex align-center flex-wrap ga-3"><h2 class="flex-grow-1">每日推送文</h2><v-chip :color="color(data.text.delivery_state)" size="small">{{names[data.text.delivery_state]}}</v-chip></div>
            <p class="text-caption text-secondary mt-3">生成时间：{{stamp(data.text.generated_at)}} · {{data.text.ready?'已就绪':'尚未就绪'}}</p>
            <v-alert v-if="data.text.reason" type="info" variant="tonal" class="my-3">{{data.text.reason}}</v-alert>
            <pre v-if="data.text.text" class="publication-text mt-4">{{data.text.text}}</pre>
            <div class="d-flex flex-wrap ga-3 mt-4">
              <v-btn color="primary" variant="outlined" prepend-icon="mdi-file-document-edit-outline" :loading="generating" :disabled="data.date!==data.today||sending||generating||!!failure" @click="generateText">生成推送文</v-btn>
              <v-btn variant="outlined" :disabled="data.date!==data.today||!data.text.ready||sending||!!failure" @click="openSend('text')">仅推送文</v-btn>
            </div>
            <p v-if="generationTask" class="text-caption text-secondary mt-3">生成任务编号：{{generationTask}}；本页会自动刷新生成结果。</p>
          </v-card>
        </v-col>
        <v-col cols="12" lg="6">
          <v-card class="pa-5 h-100">
            <div class="d-flex align-center flex-wrap ga-3"><h2 class="flex-grow-1">管道简报</h2><v-chip :color="color(data.briefing.delivery_state)" size="small">{{names[data.briefing.delivery_state]}}</v-chip></div>
            <p class="text-caption text-secondary mt-3">生成时间：{{stamp(data.briefing.generated_at)}} · {{data.briefing.images.length}} 张图片<span v-if="data.briefing.fallback_notice"> · 待分析通知</span></p>
            <v-alert v-if="data.briefing.reason" type="info" variant="tonal" class="my-3">{{data.briefing.reason}}</v-alert>
            <div v-for="(image,index) in data.briefing.images" :key="image.id" class="mt-4"><a :href="image.url" target="_blank" rel="noopener"><img class="publication-image" :src="image.url" :alt="'管道简报第'+(index+1)+'页'"/></a><p class="text-caption text-secondary mt-1">第{{index+1}}页，点击查看原图</p></div>
            <div class="d-flex flex-wrap ga-3 mt-4"><v-btn variant="outlined" :disabled="data.date!==data.today||!data.briefing.ready||sending||!!failure" @click="openSend('briefing')">仅推送管道简报</v-btn><v-btn v-if="data.briefing.download" variant="text" :href="data.briefing.download.url">下载简报 Excel</v-btn></div>
          </v-card>
        </v-col>
      </v-row>
      <v-card class="mt-5">
        <div class="pa-5"><h2>投递明细</h2><p class="section-subtitle">推送文和简报图片分别记录。相同内容已提交时跳过，旧版本或不确定结果不会自动覆盖重发。</p></div>
        <v-table v-if="data.deliveries.length"><thead><tr><th>接收会话</th><th>内容</th><th>发送状态</th><th>尝试次数</th><th>说明</th></tr></thead><tbody><tr v-for="row in data.deliveries" :key="row.recipient+':'+row.page"><td>{{row.recipient}}</td><td>{{row.page===0?'推送文':'管道简报第'+row.page+'页'}}</td><td><v-chip size="small" :color="color(row.state)">{{row.state==='sending'?'发送中／中断待核对':names[row.state]||row.state}}</v-chip></td><td>{{row.attempts}}</td><td>{{row.error||'—'}}</td></tr></tbody></v-table>
        <p v-else class="pa-5 text-secondary">该日尚无投递记录。</p>
      </v-card>
    </template>
    <v-dialog v-model="dialog" max-width="640" :persistent="sending">
      <v-card title="确认手动推送">
        <v-card-text>
          <p class="mb-4">推送日期：{{data?.date}}（北京时间）。将立即操作服务器上的微信客户端。</p>
          <v-select v-model="kind" label="推送内容" :disabled="sending" :items="[{title:'推送文和管道简报',value:'all'},{title:'仅推送文',value:'text'},{title:'仅管道简报',value:'briefing'}]"/>
          <p class="text-body-2 mb-2">按已保存的发布开关，本次接收会话：</p>
          <p v-for="receiver in receivers" :key="receiver.name" class="text-body-2 mb-2">{{receiver.name}}：{{[receiver.text?'推送文':'',receiver.briefing?'管道简报':''].filter(Boolean).join('、')}}</p>
          <v-alert v-if="!canSend" type="warning" variant="tonal" class="mt-3">所选内容尚未就绪，或未启用相应接收会话。请检查文件状态和同步配置中的发布开关。</v-alert>
          <p class="text-caption text-secondary mt-4">只推送当天内容，忽略自动推送的时间窗口。已经提交的相同内容会跳过；结果不确定或已有不同版本的发送记录会保留待核对，不自动重复发送。</p>
          <v-alert v-if="sendError" type="error" variant="tonal" class="mt-3">{{sendError}}</v-alert>
        </v-card-text>
        <v-card-actions><v-spacer/><v-btn :disabled="sending" @click="dialog=false">取消</v-btn><v-btn color="primary" :loading="sending" :disabled="!canSend" @click="send">确认推送</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<style scoped>
.publication-text { white-space:pre-wrap; overflow-wrap:anywhere; font:inherit; line-height:1.8; max-height:600px; overflow:auto; }
.publication-image { display:block; width:100%; max-height:460px; object-fit:contain; }
</style>
