<script setup>
import HelpTip from './HelpTip.vue'
import ActionIcon from './ActionIcon.vue'
import UnitTextPanel from './UnitTextPanel.vue'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
const props=defineProps({api:{type:Function,required:true}})
const emit=defineEmits(['notice'])
const data=ref(null), day=ref(''), loading=ref(false), failure=ref('')
const unitDeliveries=ref([]),refreshToken=ref(0)
const deliveryRows=computed(()=>[...(data.value?.deliveries||[]).map(r=>({...r,kind:'original'})),...unitDeliveries.value.map(r=>({...r,kind:'unit_text',page:'unit_text'}))])
const dialog=ref(false), sending=ref(false), sendError=ref(''), kind=ref('all'), task=ref('')
let request=0
const names={pending:'未发送',submitted:'已提交微信',not_sent:'发送失败／未发送',uncertain:'发送结果待核对',unknown:'发送状态读取失败',previous_or_partial:'已提交部分内容或旧版本'}
const phases={waiting:'等待准备',preparing:'正在生成',waiting_analysis:'等待分析',prepared:'已准备',sending:'正在推送',submitted:'已提交微信',uncertain:'待人工核对',skipped:'已跳过',disabled:'自动发布已关闭',not_sent:'未发送'}
const reportCards=computed(()=>data.value?[
 {key:'pipeline',title:'管道简报',kind:'briefing',drawing:false},
 {key:'drawings',title:'图纸简报',kind:'drawings',drawing:true},
].map(item=>{const fallback=data.value.briefing||{};const content=data.value[item.key]||{...fallback,images:(fallback.images||[]).filter(i=>(i.kind==='drawing')===item.drawing)};return {...item,content,ready:content.ready&&content.images.length>0}}):[])
const receivers=computed(()=>(data.value?.targets||[]).map(t=>{
  const text=t.text&&['all','text'].includes(kind.value)
  const pipeline=t.pipeline&&['all','briefing'].includes(kind.value)
  const drawings=t.drawings&&['all','drawings'].includes(kind.value)
  return {...t,text,pipeline,drawings,briefing:pipeline||drawings}
}).filter(t=>t.text||t.briefing))
const canSend=computed(()=>data.value&&data.value.date===data.value.today&&receivers.value.length>0
  &&(!receivers.value.some(t=>t.text)||data.value.text.ready)
  &&(!receivers.value.some(t=>t.pipeline)||reportCards.value[0]?.ready)
  &&(!receivers.value.some(t=>t.drawings)||reportCards.value[1]?.ready))
function stamp(value){return value?new Date(value).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'—'}
function color(state){return state==='submitted'?'success':state==='uncertain'?'warning':state==='not_sent'?'error':'secondary'}
async function load(){
  const current=++request
  loading.value=true
  try{
    const result=await props.api('/publications'+(day.value?'?date='+encodeURIComponent(day.value):''))
    if(current===request){data.value=result;failure.value='';unitDeliveries.value=[];refreshToken.value++}
  }catch(error){if(current===request)failure.value=error.message}
  finally{if(current===request)loading.value=false}
}
function openSend(value){kind.value=value;sendError.value='';dialog.value=true}
async function send(){
  if(sending.value||!canSend.value)return
  sending.value=true;sendError.value=''
  try{
    const result=await props.api('/publications/push',{method:'POST',body:JSON.stringify({date:data.value.date,kind:kind.value,confirmed:true})})
    task.value=result.id;dialog.value=false
    emit('notice','手动推送任务已提交，实际结果会在本页更新，也可查看资料解析及管理工具操作记录。')
    await load()
  }catch(error){sendError.value=error.message}
  finally{sending.value=false}
}
watch(day,load)
onMounted(load)
onUnmounted(()=>{request++})
</script>

<template>
  <div data-testid="publications-panel">
    <v-card class="mb-5 pa-5">
      <div class="d-flex align-center flex-wrap ga-3">
        <div class="flex-grow-1"><h2>每日推送</h2><p class="section-subtitle">分别查看下载日报、单元资料推送文、管道简报和图纸简报。</p></div>
        <v-text-field v-model="day" type="date" :max="data?.today" label="查看日期（留空为今天）" hide-details style="max-width:250px" :disabled="sending||dialog"/>
        <ActionIcon :loading="loading" @click="load" label="刷新" icon="mdi-refresh"/>
        <v-btn color="primary" prepend-icon="mdi-send-outline" :disabled="!data||data.date!==data.today||sending||!!failure" @click="openSend('all')">推送原日报及简报</v-btn>
      </div>
      <v-alert v-if="failure" type="error" variant="tonal" class="mt-4">{{failure}}</v-alert>
      <template v-if="data">
        <p class="text-body-2 mt-4">所选日期：{{data.date}} · 自动调度：{{phases[data.schedule.phase]||data.schedule.phase||'暂无记录'}}<span v-if="data.schedule.reason">；{{data.schedule.reason}}</span></p>
        <p v-if="data.schedule.text_publication" class="text-body-2 mt-2">推送文：{{data.schedule.text_publication.scheduled_send_time||'—'}} · {{phases[data.schedule.text_publication.phase]||data.schedule.text_publication.phase}}；管道简报：{{data.schedule.scheduled_send_time||'—'}} · {{phases[data.schedule.phase]||data.schedule.phase}}</p>
        <p class="text-caption text-secondary mt-2">“已提交微信”表示客户端发送动作已完成，不代表接收人已收到。结果不确定的记录需在微信中核对。</p>
        <v-alert v-for="error in data.errors" :key="error" type="warning" variant="tonal" class="mt-3">{{error}}</v-alert>
        <v-alert v-if="task" type="info" variant="tonal" class="mt-3">手动推送任务编号：{{task}}。请等待下方投递状态更新。</v-alert>
        <p v-if="data.manual" class="text-body-2 mt-3">最近手动推送：{{phases[data.manual.phase]||data.manual.phase}} · 开始 {{stamp(data.manual.started_at)}} · 结束 {{stamp(data.manual.ended_at)}}<span v-if="data.manual.error">；{{data.manual.error}}</span><span v-if="data.manual.result?.errors?.length">；{{data.manual.result.errors.join('；')}}</span></p>
      </template>
    </v-card>
    <template v-if="data">
      <div class="publication-grid">
        
          <v-card class="pa-5 h-100">
            <div class="d-flex align-center flex-wrap ga-3"><h2 class="flex-grow-1">每日推送文</h2><v-chip :color="color(data.text.delivery_state)" size="small">{{names[data.text.delivery_state]}}</v-chip></div>
            <p class="text-caption text-secondary mt-3">快照时间：{{stamp(data.text.generated_at)}} · {{data.text.ready?'已就绪':'尚未就绪'}}；发送使用这份快照并留存备份。</p>
            <v-alert v-if="data.text.reason" type="info" variant="tonal" class="my-3">{{data.text.reason}}</v-alert>
            <pre v-if="data.text.text" class="publication-text mt-4">{{data.text.text}}</pre>
            <div class="d-flex flex-wrap ga-3 mt-4">
              <v-btn variant="outlined" :disabled="data.date!==data.today||!data.text.ready||sending||!!failure" @click="openSend('text')">仅推送文</v-btn>
            </div>
          </v-card>
        
        
          <UnitTextPanel :api="api" :date="data.date" :refresh-token="refreshToken" compact class="h-100" @deliveries="unitDeliveries=$event" @notice="(...args)=>emit('notice',...args)"/>
          <v-card v-for="card in reportCards" :key="card.key" class="pa-5 h-100" :data-testid="card.key+'-publication-card'">
            <div class="d-flex align-center flex-wrap ga-3"><h2 class="flex-grow-1">{{card.title}}</h2><v-chip :color="color(card.content.delivery_state)" size="small">{{names[card.content.delivery_state]||'未发送'}}</v-chip></div>
            <p class="text-caption text-secondary mt-3">生成时间：{{stamp(card.content.generated_at)}} · {{card.content.images.length}} 张图片<span v-if="!card.drawing&&card.content.fallback_notice"> · 待分析通知</span></p>
            <v-alert v-if="!card.ready" type="info" variant="tonal" class="my-3">{{card.content.reason||card.title+'尚未生成'}}</v-alert>
            <div v-for="(image,index) in card.content.images" :key="image.id" class="mt-4"><a :href="image.url" target="_blank" rel="noopener"><img class="publication-image" :src="image.url" :alt="card.title+'第'+(image.page>=10001?image.page-10000:index+1)+'页'"/></a><p class="text-caption text-secondary mt-1">{{card.title}} · 第{{image.page>=10001?image.page-10000:index+1}}页，点击查看原图</p></div>
            <div class="d-flex flex-wrap ga-3 mt-4"><v-btn variant="outlined" :disabled="data.date!==data.today||!card.ready||sending||!!failure" @click="openSend(card.kind)">{{card.drawing?'仅推送图纸简报':'仅推送管道简报'}}</v-btn><ActionIcon v-if="!card.drawing&&card.content.download" :href="card.content.download.url" label="下载简报 Excel" icon="mdi-download"/></div>
          </v-card>
        
      </div>
      <v-card class="mt-5">
        <div class="pa-5"><h2>投递明细</h2><p class="section-subtitle">按接收会话查看文字和简报的投递结果。<HelpTip text="推送文和简报图片分别记录；相同内容已提交时跳过，旧版本或不确定结果不会自动覆盖重发。"/></p></div>
        <v-table v-if="deliveryRows.length"><thead><tr><th>接收会话</th><th>内容</th><th>发送状态</th><th>尝试次数</th><th>说明</th></tr></thead><tbody><tr v-for="row in deliveryRows" :key="row.kind+':'+row.recipient+':'+row.page"><td>{{row.recipient}}</td><td>{{row.kind==='unit_text'?'单元资料推送文':row.page===0?'推送文':(row.page>=10001?'图纸变更简报第'+(row.page-10000):'管道简报第'+row.page)+'页'}}</td><td><v-chip size="small" :color="color(row.state)">{{row.state==='sending'?'发送中／中断待核对':names[row.state]||row.state}}</v-chip></td><td>{{row.attempts}}</td><td>{{row.error||'—'}}</td></tr></tbody></v-table>
        <p v-else class="pa-5 text-secondary">该日尚无投递记录。</p>
      </v-card>
    </template>
    <v-dialog v-model="dialog" max-width="640" :persistent="sending">
      <v-card title="确认手动推送">
        <v-card-text>
          <p class="mb-4">推送日期：{{data?.date}}（北京时间）。将立即操作服务器上的微信客户端。</p>
          <v-select v-model="kind" label="推送内容" :disabled="sending" :items="[{title:'推送文和简报图片',value:'all'},{title:'仅推送文',value:'text'},{title:'仅管道简报',value:'briefing'},{title:'仅图纸简报',value:'drawings'}]"/>
          <p class="text-body-2 mb-2">按已保存的发布开关，本次接收会话：</p>
          <p v-for="receiver in receivers" :key="receiver.name" class="text-body-2 mb-2">{{receiver.name}}：{{[receiver.text?'推送文':'',receiver.pipeline?'管道简报':'',receiver.drawings?'图纸简报':''].filter(Boolean).join('、')}}</p>
          <v-alert v-if="!canSend" type="warning" variant="tonal" class="mt-3">所选内容尚未就绪，或未启用相应接收会话。请检查文件状态和资料解析及管理设置中的发布开关。</v-alert>
          <p class="text-caption text-secondary mt-4">只推送当天内容，忽略自动推送的时间窗口。已经提交的相同内容会跳过；结果不确定或已有不同版本的发送记录会保留待核对，不自动重复发送。</p>
          <v-alert v-if="sendError" type="error" variant="tonal" class="mt-3">{{sendError}}</v-alert>
        </v-card-text>
        <v-card-actions><v-spacer/><v-btn :disabled="sending" @click="dialog=false">取消</v-btn><v-btn color="primary" :loading="sending" :disabled="!canSend" @click="send">确认推送</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<style scoped>
.publication-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px}.publication-grid>.v-card{min-width:0}
@media(max-width:1199px){.publication-grid{grid-template-columns:minmax(0,1fr)}}
@media(max-width:700px){.publication-grid{gap:16px}}
.publication-text { white-space:pre-wrap; overflow-wrap:anywhere; font:inherit; line-height:1.8; max-height:600px; overflow:auto; }
.publication-image { display:block; width:100%; max-height:460px; object-fit:contain; }
</style>
