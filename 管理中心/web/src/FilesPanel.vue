<script setup>
import {computed,onMounted,onUnmounted,ref,watch} from 'vue'
import ActionIcon from './ActionIcon.vue'
const props=defineProps({api:Function})
const emit=defineEmits(['notice'])
const columns=[
  ['kind','记录类型',120],['parent_archive','所属压缩包',260],['member_path','包内完整路径',280],
  ['extraction_status','解压状态',180],['parent_file_id','所属压缩包内容ID',170],
  ['name','文件名称',280],['business_type','业务类型',210],['discipline','业务专业',110],
  ['plant','厂区',80],['unit','单元',110],['unit_name','单元名称',220],['reference','文档编号',250],
  ['title','文档标题',260],['revision','修订',80],['extension','扩展名',90],['relation','原件/附件类型',130],
  ['download_status','下载状态',110],['analysis_status','后台解析状态',150],['state','本机状态',120],
  ['bytes','已保存字节',125],['expected_bytes','平台文件字节',125],['downloaded_at','下载日期（北京）',195],
  ['date_source','日期来源',170],['modified','更新时间',190],['folder_path','平台目录',260],['path','本地路径',280],
  ['business_status','业务识别状态',190],['business_source','业务识别来源',150],
  ['business_basis','编码规则依据',240],['business_reason','业务待核对原因',280],['matched_number','命中编号',260],
  ['sha256','SHA256',260],['error','错误信息',280],['file_id','内容ID',160],['project','项目',160],
].map(([key,label,width])=>({key,label,width}))
const defaultColumns=['name','kind','business_type','discipline','unit','reference','revision','download_status','analysis_status','state','bytes','downloaded_at','project']
const visible=ref(defaultColumns)
try{const saved=JSON.parse(localStorage.getItem('project-file-columns'));if(Array.isArray(saved)&&saved.length)visible.value=saved.filter(key=>columns.some(c=>c.key===key))}catch{}
const shown=computed(()=>columns.filter(c=>visible.value.includes(c.key)))
const category=ref('downloads'),search=ref(''),page=ref(1),pageSize=ref(20)
const data=ref({items:[],total:0}),loading=ref(false),preview=ref(null),details=ref(null),failure=ref('')
const filters=ref({}),sort=ref('name'),direction=ref('asc')
const jumpValue=ref('1')
const pageCount=computed(()=>Math.max(1,Math.ceil(data.value.total/pageSize.value)))
const pageNumbers=computed(()=>{const start=Math.max(1,Math.min(page.value-2,pageCount.value-4));return Array.from({length:Math.min(5,pageCount.value)},(_,i)=>start+i)})
const menuOpen=ref(false),menuTarget=ref(null),activeColumn=ref(null),working=ref({})
const choices=ref([]),choiceTotal=ref(0),choiceLoading=ref(false),choiceSearch=ref(''),choiceError=ref('')
let revision=0,choiceRevision=0,debounce,facetDebounce
const categories=[{title:'文件台账（含解压文件）',value:'downloads'},{title:'简报与导出',value:'reports'},{title:'本地模拟结果',value:'simulation'}]
const operators=computed(()=>[
  {title:'包含',value:'contains'},{title:'不包含',value:'not_contains'},
  {title:'等于',value:'equals'},{title:'不等于',value:'not_equal'},
  {title:'开头是',value:'starts_with'},{title:'结尾是',value:'ends_with'},
  ...(['bytes','expected_bytes','downloaded_at','modified'].includes(activeColumn.value?.key)?[
    {title:'大于或晚于',value:'gt'},{title:'大于等于或不早于',value:'gte'},
    {title:'小于或早于',value:'lt'},{title:'小于等于或不晚于',value:'lte'},
    {title:'介于',value:'between'},]:[]),
  {title:'空白',value:'empty'},{title:'非空白',value:'not_empty'},
])
function params(extra={}){return new URLSearchParams({category:category.value,search:search.value||'',filters:JSON.stringify(filters.value),sort:sort.value,direction:direction.value,...extra})}
async function load(refresh=false){
  const version=++revision;loading.value=true
  try{const result=await props.api('/files?'+params({offset:String((page.value-1)*pageSize.value),limit:String(pageSize.value),refresh:refresh?'1':'0'}));if(version===revision){data.value=result;failure.value=''}}
  catch(e){if(version===revision)failure.value=e.message}finally{if(version===revision)loading.value=false}
}
function filter(){page.value=1;load()}
function goPage(number){if(loading.value||number===page.value)return;page.value=number;load()}
function jump(){const number=Number(jumpValue.value);if(!Number.isInteger(number)||number<1||number>pageCount.value){emit('notice','请输入 1 至 '+pageCount.value+' 之间的页码',true);return}goPage(number)}
watch(page,value=>{jumpValue.value=String(value)})
function clearFilters(){filters.value={};search.value='';sort.value='name';direction.value='asc';filter()}
function removeFilter(key){const copy={...filters.value};delete copy[key];filters.value=copy;filter()}
async function show(item){try{preview.value=await props.api('/preview?id='+item.id)}catch(e){emit('notice',e.message,true)}}
function size(n){if(n===null||n===undefined)return '—';if(n<1024)return n+' B';if(n<1024*1024)return(n/1024).toFixed(1)+' KiB';return(n/1024/1024).toFixed(1)+' MiB'}
function text(item,key){if(key==='state')return item.local_status||(item.state==='ready'?'可读取':'本机未就绪');const value=item[key];return value===null||value===undefined||value===''?'—':String(value)}
function columnName(key){return columns.find(c=>c.key===key)?.label||key}
function openFilter(column,event){
  activeColumn.value=column;menuTarget.value=event.currentTarget;working.value=JSON.parse(JSON.stringify(filters.value[column.key]||{}))
  choiceSearch.value='';choices.value=[];choiceTotal.value=0;choiceError.value='';menuOpen.value=true;loadChoices()
}
async function loadChoices(more=false){
  if(!activeColumn.value)return
  const version=++choiceRevision;choiceLoading.value=true
  try{const result=await props.api('/files?'+params({facet:activeColumn.value.key,facet_search:choiceSearch.value,offset:String(more?choices.value.length:0),limit:'100'}));if(version===choiceRevision){choices.value=more?[...choices.value,...result.items]:result.items;choiceTotal.value=result.total;choiceError.value=''}}
  catch(e){if(version===choiceRevision)choiceError.value=e.message}finally{if(version===choiceRevision)choiceLoading.value=false}
}
function selected(value){return Array.isArray(working.value.values)?working.value.values.includes(value):!(working.value.exclude_values||[]).includes(value)}
function toggle(value){const key=Array.isArray(working.value.values)?'values':'exclude_values';const set=new Set(working.value[key]||[]);set.has(value)?set.delete(value):set.add(value);working.value={...working.value,[key]:[...set]}}
function selectAll(){const copy={...working.value};delete copy.values;delete copy.exclude_values;working.value=copy}
function selectNone(){const copy={...working.value,values:[]};delete copy.exclude_values;working.value=copy}
function apply(){
  const copy={...working.value}
  if(!copy.operator||(!copy.value&&!['empty','not_empty'].includes(copy.operator))){delete copy.operator;delete copy.value;delete copy.value2}
  if(copy.exclude_values?.length===0)delete copy.exclude_values
  if(Object.keys(copy).length)filters.value={...filters.value,[activeColumn.value.key]:copy};else{const next={...filters.value};delete next[activeColumn.value.key];filters.value=next}
  menuOpen.value=false;filter()
}
function sortBy(order){sort.value=activeColumn.value.key;direction.value=order;menuOpen.value=false;filter()}
watch(category,()=>{filters.value={};sort.value='name';direction.value='asc';filter()})
watch(pageSize,filter)
watch(search,()=>{clearTimeout(debounce);debounce=setTimeout(filter,300)})
watch(choiceSearch,()=>{clearTimeout(facetDebounce);facetDebounce=setTimeout(()=>loadChoices(),250)})
watch(visible,value=>{try{localStorage.setItem('project-file-columns',JSON.stringify(value))}catch{}})
onMounted(()=>load())
onUnmounted(()=>{revision++;choiceRevision++;clearTimeout(debounce);clearTimeout(facetDebounce)})
</script>

<template>
  <v-card class="file-catalog">
    <div class="section-toolbar pa-5"><div><h2>项目文件</h2><p class="section-subtitle">点击列标题筛选或排序；多列条件共同生效，统计覆盖全部记录。</p></div><v-btn variant="tonal" prepend-icon="mdi-refresh" :loading="loading" @click="load(true)">刷新列表</v-btn></div>
    <div class="file-toolbar px-5 pb-4"><v-select v-model="category" :items="categories" label="文件分类" style="max-width:240px"/><v-text-field v-model="search" label="搜索文件名 / 项目" placeholder="也可搜索文档编号、业务类型、单元和路径" prepend-inner-icon="mdi-magnify" clearable @click:clear="search=''"/><v-select v-model="visible" :items="columns" item-title="label" item-value="key" label="显示列" multiple style="max-width:270px"><template #selection="{index}"><span v-if="index===0">已显示 {{visible.length}} 列</span></template></v-select><v-btn variant="text" prepend-icon="mdi-filter-remove-outline" @click="clearFilters">清除全部筛选</v-btn></div>
    <div v-if="Object.keys(filters).length" class="file-filter-tags px-5 pb-4"><v-chip v-for="(rule,key) in filters" :key="key" size="small" color="primary" closable :aria-label="'清除 '+columnName(key)+' 筛选'" @click:close="removeFilter(key)">{{columnName(key)}} · {{rule.values?rule.values.length+' 项':rule.exclude_values?.length?'排除 '+rule.exclude_values.length+' 项':rule.value||'空白条件'}}</v-chip></div>
    <v-alert v-if="failure" type="error" variant="tonal" class="mx-5 mb-5">{{failure}}</v-alert>
    <v-alert v-if="data.warning" type="warning" variant="tonal" class="mx-5 mb-5">{{data.warning}}</v-alert>
    <v-progress-linear v-if="loading" indeterminate color="primary"/>
    <v-table fixed-header height="60vh" class="file-grid" hover>
      <thead><tr><th v-for="column in shown" :key="column.key" :style="{minWidth:column.width+'px',maxWidth:column.width+'px'}" :class="{'file-pinned-name':column.key==='name'}"><button class="file-column-heading" :class="{'is-filtered':filters[column.key]}" :aria-label="'筛选 '+column.label" @click="openFilter(column,$event)"><span>{{column.label}}</span><v-icon v-if="sort===column.key" :icon="direction==='asc'?'mdi-arrow-up':'mdi-arrow-down'" size="14"/><v-icon :icon="filters[column.key]?'mdi-filter':'mdi-chevron-down'" size="15"/></button></th><th class="file-pinned-actions">操作</th></tr></thead>
      <tbody><tr v-for="item in data.items" :key="item.id"><td v-for="column in shown" :key="column.key" :class="{'file-pinned-name':column.key==='name'}" :style="{maxWidth:column.width+'px'}"><template v-if="column.key==='name'"><button class="file-name-link" :title="item.name" @click="details=item">{{item.name}}</button></template><v-chip v-else-if="column.key==='state'" size="small" :color="item.state==='ready'?'success':'secondary'">{{text(item,column.key)}}</v-chip><span v-else class="file-cell-text" :title="text(item,column.key)">{{text(item,column.key)}}</span><small v-if="column.key==='bytes'" class="file-byte-label">{{size(item.bytes)}}</small></td><td class="file-pinned-actions"><div class="d-flex ga-1"><ActionIcon :label="'详情 '+item.name" icon="mdi-information-outline" @click="details=item"/><ActionIcon :label="'查看 '+item.name" icon="mdi-eye-outline" :disabled="item.state!=='ready'" @click="show(item)"/><v-btn :href="'/api/file/'+item.id" :aria-label="'下载 '+item.name" icon="mdi-download" variant="tonal" size="small" color="primary" :disabled="item.state!=='ready'"/></div></td></tr><tr v-if="!data.items.length&&!loading"><td :colspan="shown.length+1"><div class="empty-state"><v-icon icon="mdi-folder-open-outline" size="42"/><h3 class="mt-3">{{search||Object.keys(filters).length?'没有匹配的文件':'此分类下暂时没有文件'}}</h3><p class="mt-2">可调整列筛选条件，或刷新列表查看最新台账。</p></div></td></tr></tbody>
    </v-table>
    <div class="pagination-bar file-pagination"><span>筛选后 {{data.total.toLocaleString()}} / {{(data.unfiltered_total??data.total).toLocaleString()}} 个文件 · 第 {{page}} / {{pageCount}} 页</span><v-select v-model="pageSize" :items="[20,50,100]" label="每页条数" density="compact" hide-details style="max-width:110px"/><div class="file-page-buttons"><v-btn variant="text" size="small" :disabled="page===1||loading" @click="goPage(page-1)">上一页</v-btn><v-btn v-for="number in pageNumbers" :key="number" class="file-page-number" size="small" :variant="page===number?'flat':'text'" :color="page===number?'primary':undefined" :aria-label="'跳到第 '+number+' 页'" :aria-current="page===number?'page':undefined" :disabled="loading" @click="goPage(number)">{{number}}</v-btn><v-btn variant="text" size="small" :disabled="page>=pageCount||loading" @click="goPage(page+1)">下一页</v-btn></div><form class="file-page-jump" @submit.prevent="jump"><label>跳至 <input v-model="jumpValue" type="number" min="1" :max="pageCount" aria-label="跳转到页码" :disabled="loading"/> 页</label><v-btn type="submit" variant="tonal" size="small" :disabled="loading">跳转</v-btn></form></div>
  </v-card>
  <v-menu v-model="menuOpen" :target="menuTarget" :close-on-content-click="false" location="bottom start" :max-width="360">
    <v-card v-if="activeColumn" class="file-filter-popup pa-4" width="340"><h3 class="mb-3">筛选：{{activeColumn.label}}</h3><div class="d-flex ga-2 mb-3"><v-btn variant="tonal" size="small" prepend-icon="mdi-sort-ascending" @click="sortBy('asc')">升序</v-btn><v-btn variant="tonal" size="small" prepend-icon="mdi-sort-descending" @click="sortBy('desc')">降序</v-btn><v-btn variant="text" size="small" @click="removeFilter(activeColumn.key);menuOpen=false">清除此列</v-btn></div><v-select v-model="working.operator" :items="operators" label="自定义条件" density="compact" clearable/><div v-if="working.operator&&!['empty','not_empty'].includes(working.operator)" class="mt-3"><v-text-field v-model="working.value" :label="['bytes','expected_bytes'].includes(activeColumn.key)?'条件值（字节）':['downloaded_at','modified'].includes(activeColumn.key)?'条件值（日期 YYYY-MM-DD）':'条件值'" density="compact"/><v-text-field v-if="working.operator==='between'" v-model="working.value2" label="上限" density="compact" class="mt-2"/></div><v-text-field v-model="choiceSearch" label="搜索筛选值" prepend-inner-icon="mdi-magnify" density="compact" class="mt-4"/><div class="file-choice-toolbar"><button @click="selectAll">全选</button><button @click="selectNone">全不选</button><span>{{choiceTotal}} 个可选值</span></div><p class="file-choice-note">搜索用于定位选项；同列勾选为“或”，不同列为“且”。</p><v-alert v-if="choiceError" type="error" density="compact">{{choiceError}}</v-alert><div class="file-filter-choices"><label v-for="choice in choices" :key="choice.value" class="file-filter-choice"><input type="checkbox" :checked="selected(choice.value)" @change="toggle(choice.value)"/><span :title="choice.value">{{choice.value||'(空白)'}}</span><small>{{choice.count}}</small></label><p v-if="!choices.length&&!choiceLoading" class="pa-3 text-secondary">没有可选值</p><v-btn v-if="choices.length<choiceTotal" variant="text" block size="small" :loading="choiceLoading" @click="loadChoices(true)">加载更多（{{choices.length}} / {{choiceTotal}}）</v-btn></div><v-progress-linear v-if="choiceLoading" indeterminate class="mt-2"/><div class="d-flex justify-end ga-2 mt-4"><v-btn variant="text" @click="menuOpen=false">取消</v-btn><v-btn color="primary" @click="apply">应用筛选</v-btn></div></v-card>
  </v-menu>
  <v-dialog :model-value="!!details" @update:model-value="details=null" max-width="1100" scrollable aria-labelledby="file-details-title">
    <v-card v-if="details" class="file-dialog-card">
      <v-card-item class="file-dialog-header">
        <template #prepend><v-icon icon="mdi-file-document-outline" color="primary"/></template>
        <template #title><h2 id="file-details-title" class="text-subtitle-1 font-weight-bold">文件详情</h2></template>
        <template #subtitle><span :title="details.name">{{details.name}}</span></template>
        <template #append><v-btn icon="mdi-close" variant="text" size="small" aria-label="关闭文件详情" @click="details=null"/></template>
      </v-card-item>
      <v-divider/>
      <v-card-text class="file-dialog-body" tabindex="0" role="region" aria-label="文件详情内容">
        <dl class="file-details-grid"><div v-for="column in columns" :key="column.key"><dt>{{column.label}}</dt><dd>{{text(details,column.key)}}</dd></div></dl>
      </v-card-text>
    </v-card>
  </v-dialog>
  <v-dialog :model-value="!!preview" @update:model-value="preview=null" max-width="1000" scrollable aria-labelledby="file-preview-title">
    <v-card v-if="preview" class="file-dialog-card">
      <v-card-item class="file-dialog-header">
        <template #prepend><v-icon icon="mdi-eye-outline" color="primary"/></template>
        <template #title><h2 id="file-preview-title" class="text-subtitle-1 font-weight-bold">文件预览</h2></template>
        <template #subtitle><span :title="preview.name">{{preview.name}}</span></template>
        <template #append><v-btn icon="mdi-close" variant="text" size="small" aria-label="关闭预览" @click="preview=null"/></template>
      </v-card-item>
      <v-divider/>
      <v-card-text class="file-dialog-body" tabindex="0" role="region" aria-label="文件预览内容">
        <img v-if="preview.kind==='image'" :src="preview.url" :alt="preview.name" class="file-preview-image"/>
        <pre v-else class="file-preview">{{preview.text}}</pre>
        <p v-if="preview.truncated" class="text-caption text-secondary mt-3">显示前128 KiB，完整内容请下载原件。</p>
      </v-card-text>
    </v-card>
  </v-dialog>
</template>

<style scoped>
/* Keep Vuetify's fixed header above the pinned body cells (z-index: 2). */
.file-grid :deep(thead) { z-index: 3; }
.v-dialog .v-card.file-dialog-card {
  max-height: min(90vh, 90dvh);
  overflow: hidden;
}
.file-dialog-header {
  flex: 0 0 auto;
  padding: 16px 20px;
}
.file-dialog-header :deep(.v-card-item__content) { min-width: 0; }
.file-dialog-header h2 { margin: 0; font-size: 16px; line-height: 24px; }
.file-dialog-header :deep(.v-card-item__append) { padding-inline-start: 16px; }
.file-dialog-header :deep(.v-card-subtitle) { margin-top: 2px; }
.file-dialog-body {
  min-height: 0;
  padding: 24px;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  scrollbar-width: thin;
  scrollbar-color: rgba(var(--v-theme-secondary), .4) transparent;
}
.file-dialog-body::-webkit-scrollbar { width: 8px; }
.file-dialog-body::-webkit-scrollbar-track { background: transparent; }
.file-dialog-body::-webkit-scrollbar-thumb {
  background: rgba(var(--v-theme-secondary), .35);
  border: 2px solid rgb(var(--v-theme-surface));
  border-radius: 8px;
}
.file-dialog-body::-webkit-scrollbar-thumb:hover { background: rgba(var(--v-theme-secondary), .6); }
.file-dialog-body:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: -2px; }
.file-dialog-body .file-preview { max-height: none; overflow: visible; margin: 0; }
.file-dialog-body .file-preview-image { max-height: none; height: auto; }
.file-details-grid, .file-details-grid dd { margin: 0; }
@media (max-width: 600px) {
  .file-dialog-header { padding: 12px 16px; }
  .file-dialog-body { padding: 16px; }
  .file-dialog-body .file-preview { padding: 12px; }
}
</style>
