<script setup>
import { computed, ref } from 'vue'
const props = defineProps({ modelValue: Array, label: String, hint: String, disabled: Boolean, ordered: Boolean, allowDuplicates: Boolean, compact: Boolean })
const emit = defineEmits(['update:modelValue'])
const values = computed(() => props.modelValue || [])
const opened = ref(false), editing = ref(-1), draft = ref(''), error = ref('')
function open(index = -1) {
  editing.value = index
  draft.value = index < 0 ? '' : values.value[index]
  error.value = ''
  opened.value = true
}
function commit() {
  const additions = editing.value < 0 ? draft.value.split(/\r?\n/).map(v => v.trim()).filter(Boolean) : [draft.value.trim()]
  if (!additions.length || additions.some(v => !v)) { error.value = '请填写内容'; return }
  const next = [...values.value]
  if (editing.value < 0) next.push(...additions)
  else next.splice(editing.value, 1, ...additions)
  if (!props.allowDuplicates && new Set(next).size !== next.length) { error.value = '存在重复项，请检查后再保存'; return }
  emit('update:modelValue', next)
  opened.value = false
}
function remove(index) { emit('update:modelValue', values.value.filter((_, i) => i !== index)) }
function move(index, offset) {
  const next = [...values.value]
  ;[next[index], next[index + offset]] = [next[index + offset], next[index]]
  emit('update:modelValue', next)
}
</script>
<template>
  <section class="value-list" :class="{'value-list-compact':compact}" :aria-label="label" :aria-disabled="disabled">
    <div class="value-list-heading"><div><h3>{{label}} <span class="value-list-count">{{values.length}} 项</span></h3><p v-if="hint">{{hint}}</p></div><v-btn variant="tonal" size="small" prepend-icon="mdi-plus" :disabled="disabled" @click="open()">添加</v-btn></div>
    <ol v-if="values.length" class="value-list-rows">
      <li v-for="(value, index) in values" :key="index" class="value-list-row">
        <span v-if="ordered" class="value-list-index">{{index + 1}}</span>
        <div class="value-list-content"><span class="value-list-value">{{value}}</span><slot name="item" :value="value" :index="index"/></div>
        <div class="value-list-actions">
          <template v-if="ordered"><v-btn icon="mdi-arrow-up" variant="text" size="x-small" :aria-label="'上移第 '+(index+1)+' 项'" :disabled="disabled||index===0" @click="move(index,-1)"/><v-btn icon="mdi-arrow-down" variant="text" size="x-small" :aria-label="'下移第 '+(index+1)+' 项'" :disabled="disabled||index===values.length-1" @click="move(index,1)"/></template>
          <v-btn icon="mdi-pencil-outline" variant="text" size="small" :aria-label="'编辑 '+value" :disabled="disabled" @click="open(index)"/>
          <v-btn icon="mdi-close" variant="text" size="small" :aria-label="'移除 '+value" :disabled="disabled" @click="remove(index)"/>
        </div>
      </li>
    </ol>
    <p v-else class="value-list-empty">尚未添加{{label}}，点击“添加”设置。</p>
    <v-dialog v-model="opened" max-width="560"><v-card class="pa-6"><h2 class="mb-2">{{editing<0?'添加':'编辑'}}{{label}}</h2><p v-if="editing<0" class="text-body-2 text-secondary mb-5">可一次添加多项，每行填写一个完整值。</p><v-textarea v-if="editing<0" v-model="draft" label="新增内容" rows="5" autofocus :error-messages="error"/><v-text-field v-else v-model="draft" label="内容" autofocus :error-messages="error" @keydown.enter.prevent="commit"/><v-card-actions><v-spacer/><v-btn @click="opened=false">取消</v-btn><v-btn color="primary" variant="flat" :disabled="disabled||!draft.trim()" @click="commit">{{editing<0?'添加到列表':'确认修改'}}</v-btn></v-card-actions></v-card></v-dialog>
  </section>
</template>
<style scoped>
.value-list{min-width:0;border:1px solid rgba(var(--v-theme-on-surface),.13);border-radius:12px;overflow:hidden}
.value-list-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;padding:16px 18px;background:rgba(var(--v-theme-on-surface),.025)}
.value-list-heading h3{font-size:14px;font-weight:600;line-height:1.8}.value-list-heading p{font-size:12px;color:rgb(var(--v-theme-secondary));margin:4px 0 0;line-height:1.7}.value-list-count{font-size:12px;font-weight:400;color:rgb(var(--v-theme-secondary));margin-left:8px;white-space:nowrap}
.value-list-rows{list-style:none;padding:0;margin:0;max-height:380px;overflow:auto}.value-list-row{display:flex;align-items:center;gap:12px;padding:12px 14px 12px 18px;border-top:1px solid rgba(var(--v-theme-on-surface),.08)}
.value-list-content{flex:1;min-width:0}.value-list-value{font-size:14px;overflow-wrap:anywhere;white-space:pre-wrap}.value-list-actions{display:flex;align-items:center;flex-shrink:0}.value-list-index{font-size:12px;color:rgb(var(--v-theme-secondary));min-width:20px}.value-list-empty{padding:22px 18px;font-size:13px;color:rgb(var(--v-theme-secondary));margin:0}
.value-list-compact .value-list-heading{padding:12px 14px;align-items:center}.value-list-compact .value-list-row{padding:6px 10px 6px 14px;min-height:44px}.value-list-compact .value-list-actions .v-btn{width:28px;height:28px}.value-list-compact .value-list-rows{max-height:220px}.value-list-compact .value-list-empty{padding:16px 14px}
@media(max-width:600px){.value-list-heading{padding:14px;gap:10px}.value-list-row{padding:12px;gap:6px;flex-wrap:wrap}.value-list-actions{margin-left:auto}.value-list-content{min-width:120px}.value-list-heading p{max-width:240px}}
</style>
