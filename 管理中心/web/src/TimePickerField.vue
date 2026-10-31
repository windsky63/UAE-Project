<script setup>
import { ref } from 'vue'

const props = defineProps({ modelValue: String, label: String, disabled: Boolean })
const emit = defineEmits(['update:modelValue'])
const open = ref(false)
const draft = ref('00:00')
const viewMode = ref('hour')

function show() {
  if (props.disabled) return
  draft.value = props.modelValue || '00:00'
  viewMode.value = 'hour'
  open.value = true
}
function confirm() {
  if (props.disabled || !/^([01]\d|2[0-3]):[0-5]\d$/.test(draft.value)) return
  emit('update:modelValue', draft.value)
  open.value = false
}
</script>

<template>
  <v-text-field
    :model-value="modelValue" :label="label" :disabled="disabled"
    :aria-label="label" aria-haspopup="dialog" :aria-expanded="open"
    readonly append-inner-icon="mdi-clock-outline" class="time-picker-field"
    @click="show" @keydown.enter.prevent="show" @keydown.space.prevent="show"
  />
  <v-dialog v-model="open" max-width="360" :aria-label="label">
    <v-card>
      <v-time-picker
        v-model="draft" v-model:view-mode="viewMode" :title="label" :disabled="disabled"
        format="24hr" color="primary" width="100%" :border="false"
      />
      <v-divider/>
      <v-card-actions>
        <span class="text-caption text-secondary ml-2">北京时间 · 24 小时制</span>
        <v-spacer/>
        <v-btn @click="open=false">取消</v-btn>
        <v-btn color="primary" variant="flat" :disabled="disabled||!draft" @click="confirm">确定</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style scoped>
.time-picker-field :deep(input), .time-picker-field :deep(.v-field) { cursor: pointer; }
</style>
