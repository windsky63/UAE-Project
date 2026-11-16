<script setup>
import {computed} from 'vue'
import ScopeToggleList from './ScopeToggleList.vue'
const props=defineProps({modelValue:Object,options:Object,allowed:Object,disabled:Boolean})
const emit=defineEmits(['update:modelValue'])
const scope=computed(()=>({disciplines:[],units:[],drawing_types:null,file_formats:['PDF'],...props.modelValue}))
const catalog=computed(()=>props.options?.type_catalog||{})
const allTypes=computed(()=>[...new Set(Object.values(catalog.value).flat())].sort())
const enabledTypes=computed(()=>[...new Set(scope.value.disciplines.flatMap(v=>catalog.value[v]||[]))].filter(v=>props.allowed?.drawing_types==null||props.allowed.drawing_types.includes(v)))
const selectedTypes=computed(()=>scope.value.drawing_types??enabledTypes.value)
function update(key,value){const next={...scope.value,[key]:value};if(key==='disciplines'&&next.drawing_types!==null){const types=new Set(value.flatMap(v=>catalog.value[v]||[]));next.drawing_types=next.drawing_types.filter(v=>types.has(v))}emit('update:modelValue',next)}
</script>
<template>
 <div class="responsibility-fields">
  <ScopeToggleList :model-value="scope.disciplines" @update:model-value="v=>update('disciplines',v)" :options="options?.disciplines||Object.keys(catalog)" :allowed-options="allowed?.disciplines" label="负责专业" :disabled="disabled" collapsible flat/>
  <ScopeToggleList :model-value="scope.units" @update:model-value="v=>update('units',v)" :options="options?.units||[]" :allowed-options="allowed?.units" label="负责单元" :disabled="disabled" collapsible flat/>
  <ScopeToggleList :model-value="selectedTypes" @update:model-value="v=>update('drawing_types',v)" :options="allTypes" :allowed-options="enabledTypes" label="业务图纸类型" :disabled="disabled" collapsible flat/>
  <ScopeToggleList :model-value="scope.file_formats" @update:model-value="v=>update('file_formats',v)" :options="options?.file_formats||['PDF','DWG','DXF']" :allowed-options="allowed?(allowed.file_formats??['PDF']):undefined" label="文件格式" :disabled="disabled" collapsible flat/>
 </div>
</template>
<style scoped>
.responsibility-fields{border-top:1px solid rgba(var(--v-theme-on-surface),.12);border-bottom:1px solid rgba(var(--v-theme-on-surface),.12)}
.responsibility-fields :deep(.scope-toggle-list + .scope-toggle-list){border-top:1px solid rgba(var(--v-theme-on-surface),.12)}
</style>
