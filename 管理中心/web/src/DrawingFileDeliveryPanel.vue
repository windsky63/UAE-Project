<script setup>
import {ref,onMounted,computed} from 'vue'
import {VCardTitle} from 'vuetify/components'
import TimePickerField from './TimePickerField.vue'
import ScopeToggleList from './ScopeToggleList.vue'
import ResponsibilityScopeFields from './ResponsibilityScopeFields.vue'
const props=defineProps({api:Function,responsibilityScope:Object})
const emit=defineEmits(['notice'])
const deliveries=ref([]),status=ref({}),catalog=ref({}),scopeOptions=ref({disciplines:[],units:[]})
const sharedScope=ref({disciplines:[],units:[]}),data=ref(null),revision=ref(''),busy=ref(false),error=ref('')
const draft=ref(null),draftError=ref('')
const responsible=computed(()=>props.responsibilityScope??sharedScope.value)
const globalDisciplines=computed(()=>data.value?.scope_mode==='custom'?data.value.scope.disciplines:responsible.value.disciplines)
function typesFor(disciplines){return [...new Set(disciplines.flatMap(name=>catalog.value[name]||[]))].sort()}
const globalTypes=computed(()=>typesFor(globalDisciplines.value||[]).filter(v=>responsible.value.drawing_types==null||responsible.value.drawing_types.includes(v)))
const globalUnits=computed(()=>data.value?.scope_mode==='custom'?data.value.scope.units:responsible.value.units)
const globalScope=computed(()=>({disciplines:[...globalDisciplines.value],units:[...globalUnits.value],drawing_types:[...globalTypes.value],file_formats:[...(responsible.value.file_formats??['PDF'])]}))
const editorOptions=computed(()=>({...scopeOptions.value,type_catalog:catalog.value}))
function separate(name){return ['recipient_disciplines','recipient_types','recipient_units','recipient_formats'].some(k=>data.value[k]?.[name]!=null)}
function summary(name){if(!separate(name))return '沿用全局负责范围';const ds=data.value.recipient_disciplines[name]??globalDisciplines.value;const ts=data.value.recipient_types[name];return '专业：'+(ds.join('、')||'未开启')+'；图纸类型：'+(ts===null||ts===undefined?'沿用负责范围':ts.length+' 类')}
function edit(name=''){
 const scope=JSON.parse(JSON.stringify(globalScope.value))
 for(const [field,key] of [['recipient_disciplines','disciplines'],['recipient_types','drawing_types'],['recipient_units','units'],['recipient_formats','file_formats']]){
  if(data.value[field]?.[name]!=null)scope[key]=data.value[field][name].filter(v=>scope[key].includes(v))
 }
 draftError.value='';draft.value={original:name,name,custom:name?separate(name):false,scope}
}
function setDraftCustom(value){
 draft.value.custom=value
 if(value)draft.value.scope=JSON.parse(JSON.stringify(globalScope.value))
}
async function load(){busy.value=true;try{const r=await props.api('/config?tool=drawing_delivery');data.value={scope_mode:'responsibility',scope:{disciplines:[],units:[]},drawing_types:null,recipient_types:{},recipient_disciplines:{},recipient_units:{},recipient_formats:{},...r.data};catalog.value=r.drawing_type_catalog||{};scopeOptions.value=r.scope_options||{disciplines:Object.keys(catalog.value),units:[]};sharedScope.value=r.responsibility_scope||{disciplines:[],units:[]};status.value=r.status||{};deliveries.value=r.deliveries||[];revision.value=r.revision;error.value=''}catch(e){error.value=e.message}finally{busy.value=false}}
async function save(next=data.value){busy.value=true;try{const r=await props.api('/config',{method:'POST',body:JSON.stringify({tool:'drawing_delivery',data:next,revision:revision.value})});data.value=r.data;revision.value=r.revision;error.value='';emit('notice','图纸文件发送设置已保存');return true}catch(e){error.value=e.message;return false}finally{busy.value=false}}
async function commit(){
 const name=draft.value.name.trim(),old=draft.value.original
 if(!name||name.includes('\n')){draftError.value='请填写完整群聊名称';return}
 if(name!==old&&data.value.recipients.includes(name)){draftError.value='群聊名称已存在';return}
 const next=JSON.parse(JSON.stringify(data.value))
 if(old)next.recipients=next.recipients.map(v=>v===old?name:v);else next.recipients.push(name)
 for(const [field,key] of [['recipient_disciplines','disciplines'],['recipient_types','drawing_types'],['recipient_units','units'],['recipient_formats','file_formats']]){
  delete next[field][old];next[field][name]=draft.value.custom?[...(draft.value.scope[key]??globalScope.value[key])]:null
 }
 if(await save(next))draft.value=null;else draftError.value=error.value
}
async function remove(name){const next=JSON.parse(JSON.stringify(data.value));next.recipients=next.recipients.filter(v=>v!==name);for(const field of ['recipient_disciplines','recipient_types','recipient_units','recipient_formats'])delete next[field][name];await save(next)}
onMounted(load)
</script>
<template>
 <v-card class="settings-section drawing-delivery">
  <h2>定时发送图纸文件</h2><p class="section-subtitle mb-5">全局图纸类型统一使用上方“图纸负责范围”的开关设置；各群可在编辑弹窗中进一步缩小范围。</p>
  <v-alert v-if="error" type="error" variant="tonal" class="mb-5">{{error}}</v-alert>
  <div v-if="data" class="settings-form">
   <div class="settings-pair align-center"><v-switch v-model="data.enabled" label="启用定时发送" :disabled="busy"/><TimePickerField v-model="data.send_time_beijing" label="每日发送时间（北京）" :disabled="busy"/></div>
   <section aria-label="发送范围来源"><v-switch :model-value="data.scope_mode==='responsibility'" label="沿用负责专业和单元" :disabled="busy" @update:model-value="v=>data.scope_mode=v?'responsibility':'custom'"/>
    <div v-if="data.scope_mode==='custom'" class="scope-parts"><ScopeToggleList v-model="data.scope.disciplines" :options="scopeOptions.disciplines" label="发送专业" :disabled="busy"/><ScopeToggleList v-model="data.scope.units" :options="scopeOptions.units" label="发送单元" :disabled="busy"/></div>
   </section>
   <section class="chat-recipients" aria-label="图纸接收群聊">
    <div class="group-heading"><h3>图纸接收群聊</h3><v-btn variant="tonal" prepend-icon="mdi-plus" :disabled="busy" @click="edit()">添加群聊</v-btn></div>
    <article v-for="recipient in data.recipients" :key="recipient" class="recipient-row"><div class="recipient-text"><strong>{{recipient}}</strong><p>{{summary(recipient)}}</p></div><div class="recipient-actions"><v-btn variant="text" prepend-icon="mdi-pencil-outline" :aria-label="'编辑 '+recipient" :disabled="busy" @click="edit(recipient)">编辑</v-btn><v-btn icon="mdi-delete-outline" variant="text" size="small" :aria-label="'移除 '+recipient" :disabled="busy" @click="remove(recipient)"/></div></article>
    <p v-if="!data.recipients.length" class="text-body-2 text-secondary pa-4">尚未添加接收群聊。</p>
   </section>
   <div class="settings-actions"><v-btn variant="outlined" :disabled="busy" @click="load">刷新发送记录</v-btn><v-btn :loading="busy" color="primary" @click="save()">保存图纸发送设置</v-btn></div>
   <p v-if="status.at" class="text-caption text-secondary">最近检查：{{status.at}} · {{status.state}}</p>
   <v-table v-if="deliveries.length"><thead><tr><th>群聊</th><th>文件</th><th>状态</th></tr></thead><tbody><tr v-for="(item,i) in deliveries" :key="i"><td>{{item.chat}}</td><td>{{JSON.parse(item.payload).filename}}</td><td>{{({submitted:'已提交',baseline:'初始基线',uncertain:'待人工核对',not_sent:'未发送'})[item.state]||item.state}}</td></tr></tbody></v-table>
  </div>
 </v-card>
 <v-dialog :model-value="!!draft" @update:model-value="value=>{if(!value&&!busy)draft=null}" max-width="1080" scrollable :persistent="busy">
  <v-card v-if="draft" class="chat-scope-dialog">
   <v-card-title class="dialog-header"><span>{{draft.original?'编辑群聊负责范围':'添加图纸接收群聊'}}</span><v-btn icon="mdi-close" variant="text" size="small" aria-label="关闭群聊编辑" :disabled="busy" @click="draft=null"/></v-card-title>
   <v-card-text><v-alert v-if="draftError" type="error" variant="tonal" class="mb-4">{{draftError}}</v-alert>
    <div class="dialog-settings-row"><v-text-field v-model="draft.name" label="群聊完整名称" :disabled="busy"/><div class="dialog-mode"><v-switch :model-value="draft.custom" @update:model-value="setDraftCustom" label="单独设置本群负责范围" :disabled="busy"/></div></div>
    <p class="dialog-help">{{draft.custom?'已复制全局选中项。完整列表同时显示已选和未选项，灰色项未在全局开启。':'开启单独设置后，复制全局的专业、单元、业务图纸类型和文件格式。'}}</p>
    <ResponsibilityScopeFields v-if="draft.custom" v-model="draft.scope" :options="editorOptions" :allowed="globalScope" :disabled="busy"/>
   </v-card-text><v-card-actions><v-spacer/><v-btn :disabled="busy" @click="draft=null">取消</v-btn><v-btn color="primary" variant="flat" :loading="busy" @click="commit">保存群聊设置</v-btn></v-card-actions>
  </v-card>
 </v-dialog>
</template>
<style scoped>
.scope-parts{display:grid;gap:16px}.group-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px;flex-wrap:wrap}.group-heading h3{font-size:15px}
.chat-recipients{border:1px solid rgba(var(--v-theme-on-surface),.12);border-radius:12px;overflow:hidden}.recipient-row{display:flex;gap:16px;align-items:center;justify-content:space-between;padding:16px;border-top:1px solid rgba(var(--v-theme-on-surface),.1)}
.recipient-text{min-width:0;overflow-wrap:anywhere}.recipient-text p{font-size:12px;color:rgb(var(--v-theme-secondary));margin-top:6px}.recipient-actions{display:flex;flex-shrink:0;align-items:center}
.chat-scope-dialog :deep(.v-card-title){padding:20px 24px;white-space:normal}.chat-scope-dialog :deep(.v-card-actions){padding:16px 24px}
.dialog-header{display:flex;align-items:center;justify-content:space-between;gap:16px;border-bottom:1px solid rgba(var(--v-theme-on-surface),.1)}
.dialog-settings-row{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);gap:20px;align-items:center;margin-top:12px}
.dialog-mode{padding:12px 16px;background:rgba(var(--v-theme-primary),.04);border-radius:10px}
.dialog-help{font-size:13px;line-height:1.8;color:rgb(var(--v-theme-secondary));margin:16px 0 20px}
.dialog-scope-columns{display:grid;grid-template-columns:minmax(220px,.75fr) minmax(0,1.6fr);gap:16px;align-items:start}
.dialog-professions :deep(.toggle-options){grid-template-columns:1fr;max-height:380px}
.dialog-types :deep(.toggle-options){max-height:380px}
.chat-scope-dialog :deep(.v-card-actions){border-top:1px solid rgba(var(--v-theme-on-surface),.1)}
@media(max-width:700px){.dialog-settings-row,.dialog-scope-columns{grid-template-columns:1fr}.dialog-settings-row{gap:12px}.dialog-professions :deep(.toggle-options){max-height:220px}}
@media(max-width:600px){.recipient-row{align-items:flex-start;flex-wrap:wrap}.recipient-actions{margin-left:auto}.chat-scope-dialog :deep(.v-card-text){padding:16px}}
</style>
