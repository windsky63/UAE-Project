<script setup>
import {onMounted,ref} from 'vue'
import ResponsibilityScopeFields from './ResponsibilityScopeFields.vue'
const props=defineProps({api:Function})
const emit=defineEmits(['notice','scope'])
const data=ref(null),options=ref({disciplines:[],units:[],type_catalog:{}}),revision=ref(''),busy=ref(false),error=ref('')
function receive(r){data.value={drawing_types:null,file_formats:['PDF'],...r.data};options.value=r.options||options.value;revision.value=r.revision;emit('scope',JSON.parse(JSON.stringify(data.value)))}
async function load(){busy.value=true;try{receive(await props.api('/config?tool=drawing_scope'));error.value=''}catch(e){error.value=e.message}finally{busy.value=false}}
async function save(){busy.value=true;try{receive(await props.api('/config',{method:'POST',body:JSON.stringify({tool:'drawing_scope',data:data.value,revision:revision.value})}));error.value='';emit('notice','图纸负责范围已保存，Ima、图纸简报及图纸文件发送共用专业、单元和类型限制。')}catch(e){error.value=e.message;emit('notice',e.message,true)}finally{busy.value=false}}
onMounted(load)
</script>
<template>
  <v-card class="settings-section drawing-scope">
    <h2>图纸负责范围</h2>
    <p class="text-caption text-secondary mb-4">统一选择专业、单元、业务图纸类型和文件格式。文件格式默认仅开启 PDF。</p>
    <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{error}}</v-alert>
    <ResponsibilityScopeFields v-if="data" v-model="data" :options="options" :disabled="busy"/>
    <div class="d-flex flex-wrap ga-3 mt-5"><v-btn color="primary" :disabled="!data" :loading="busy" @click="save">保存负责范围</v-btn><v-btn variant="outlined" :disabled="busy" @click="load">重新读取</v-btn></div>
  </v-card>
</template>
<style scoped>
.scope-fields{border-top:1px solid rgba(var(--v-theme-on-surface),.12);border-bottom:1px solid rgba(var(--v-theme-on-surface),.12)}
.scope-fields :deep(.scope-toggle-list + .scope-toggle-list){border-top:1px solid rgba(var(--v-theme-on-surface),.12)}
</style>
