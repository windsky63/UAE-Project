<script setup>
import {computed,ref,useId} from 'vue'
const props=defineProps({modelValue:Array,options:Array,label:String,disabled:Boolean,collapsible:Boolean,allowedOptions:Array,flat:Boolean})
const emit=defineEmits(['update:modelValue'])
defineOptions({inheritAttrs:false})
const search=ref('')
const expanded=ref(!props.collapsible),confirmation=ref(null),contentId=useId()
const selected=computed(()=>props.modelValue||[])
const choices=computed(()=>[...new Set([...(props.options||[]),...selected.value])])
const enabledChoices=computed(()=>choices.value.filter(v=>!props.allowedOptions||props.allowedOptions.includes(v)))
const visible=computed(()=>choices.value.filter(v=>v.toLowerCase().includes((search.value||'').trim().toLowerCase())))
function toggle(value,on){if(props.disabled||props.allowedOptions&&!props.allowedOptions.includes(value))return;emit('update:modelValue',on?[...new Set([...selected.value,value])]:selected.value.filter(v=>v!==value))}
function confirmBulk(){if(props.disabled)return;emit('update:modelValue',confirmation.value==='on'?[...enabledChoices.value]:[]);confirmation.value=null}
</script>
<template>
  <section v-bind="$attrs" class="scope-toggle-list" :class="{'scope-toggle-flat':flat}" :aria-label="label">
    <div class="toggle-heading">
      <button v-if="collapsible" type="button" class="scope-disclosure" :aria-expanded="expanded" :aria-controls="contentId" @click="expanded=!expanded"><span>{{label}} <small>已选 {{selected.length}} 项</small></span><v-icon :icon="expanded?'mdi-chevron-up':'mdi-chevron-down'" size="20"/></button>
      <h3 v-else>{{label}} <small>已选 {{selected.length}} 项</small></h3>
    </div>
    <div v-if="!collapsible||expanded" :id="contentId">
    <div class="bulk-actions"><v-btn size="small" variant="text" :disabled="disabled||!enabledChoices.length" @click="confirmation='on'">全部开启</v-btn><v-btn size="small" variant="text" :disabled="disabled" @click="confirmation='off'">全部关闭</v-btn></div>
    <v-text-field v-if="choices.length>10" v-model="search" :label="'筛选'+label" prepend-inner-icon="mdi-magnify" density="compact" clearable @click:clear="search=''" hide-details class="mb-3"/>
    <div class="toggle-options"><v-switch v-for="value in visible" :key="value" :model-value="selected.includes(value)" :label="value" :aria-label="label+'：'+value" :disabled="disabled||(allowedOptions&&!allowedOptions.includes(value))" @update:model-value="on=>toggle(value,on)"/></div>
    <p v-if="!visible.length" class="text-caption text-secondary">{{choices.length?'没有匹配选项。':'暂无可选项，请先开启对应专业。'}}</p>
    </div>
  </section>
  <v-dialog :model-value="!!confirmation" @update:model-value="v=>{if(!v)confirmation=null}" max-width="440">
    <v-card :title="'确认'+(confirmation==='on'?'全部开启':'全部关闭')">
      <v-card-text>确定{{confirmation==='on'?'开启':'关闭'}}“{{label}}”{{confirmation==='on'?'中全部 '+enabledChoices.length+' 个可选项':'中全部已选项'}}吗？<p class="text-caption text-secondary mt-2">确认后仍需保存当前设置才会生效。</p></v-card-text>
      <v-card-actions><v-spacer/><v-btn @click="confirmation=null">取消</v-btn><v-btn :color="confirmation==='off'?'error':'primary'" variant="flat" :disabled="disabled" @click="confirmBulk">确认{{confirmation==='on'?'全部开启':'全部关闭'}}</v-btn></v-card-actions>
    </v-card>
  </v-dialog>
</template>
<style scoped>
.scope-toggle-list{padding:16px;border:1px solid rgba(var(--v-theme-on-surface),.12);border-radius:12px;min-width:0}
.scope-toggle-list.scope-toggle-flat{padding:18px 0;border:0;border-radius:0}
.toggle-heading{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.toggle-heading h3{font-size:14px}.toggle-heading small{font-weight:400;color:rgb(var(--v-theme-secondary));margin-left:6px}
.scope-disclosure{display:flex;align-items:center;justify-content:space-between;gap:12px;width:100%;text-align:left;font:inherit;font-size:14px;font-weight:600;border:0;background:transparent;color:inherit;appearance:none;border-radius:6px;padding:2px;cursor:pointer}
.scope-disclosure:focus-visible{outline:2px solid rgb(var(--v-theme-primary));outline-offset:4px}.bulk-actions{display:flex;justify-content:flex-end;gap:4px;margin-bottom:8px}
.scope-toggle-list:has(.scope-disclosure[aria-expanded="false"]) .toggle-heading{margin-bottom:0}
.toggle-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:4px 16px;max-height:320px;overflow:auto}
.toggle-options :deep(.v-label){white-space:normal;font-size:13px;opacity:1}
@media(max-width:600px){.scope-toggle-list{padding:12px}.toggle-options{grid-template-columns:1fr}}
</style>
