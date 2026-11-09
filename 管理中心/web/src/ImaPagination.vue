<script setup>
import { computed } from 'vue'
const props = defineProps({ total: { type: Number, default: 0 }, page: { type: Number, default: 1 }, size: { type: Number, default: 20 }, loading: Boolean, fixedSize: Boolean, label: { type: String, required: true } })
const emit = defineEmits(['page', 'size'])
const last = computed(() => Math.max(1, Math.ceil(props.total / props.size)))
const pages = computed(() => {
  const values = [...new Set([1, props.page - 1, props.page, props.page + 1, last.value])].filter(value => value > 0 && value <= last.value).sort((a, b) => a - b)
  return values.flatMap((value, index) => index && value - values[index - 1] > 1 ? ['gap-' + value, value] : [value])
})
</script>
<template>
  <nav class="ima-pagination" :aria-label="label + '分页'">
    <div class="ima-page-count" aria-live="polite">共 {{ total }} 项<span v-if="total"> · 第 {{ (page - 1) * size + 1 }}–{{ Math.min(page * size, total) }} 项</span></div>
    <label v-if="!fixedSize" class="ima-page-size">每页 <select :aria-label="label + '每页条数'" :value="size" :disabled="loading" @change="emit('size', Number($event.target.value))"><option v-for="value in [20, 50, 100]" :key="value" :value="value">{{ value }} 条</option></select></label>
    <div class="ima-page-buttons"><v-btn variant="text" size="small" :disabled="loading || page <= 1" @click="emit('page', page - 1)">上一页</v-btn><template v-for="value in pages" :key="value"><span v-if="typeof value === 'string'">…</span><v-btn v-else :variant="value === page ? 'flat' : 'text'" :color="value === page ? 'primary' : undefined" size="small" :disabled="loading" :aria-label="label + '第' + value + '页'" :aria-current="value === page ? 'page' : undefined" @click="emit('page', value)">{{ value }}</v-btn></template><v-btn variant="text" size="small" :disabled="loading || page >= last" @click="emit('page', page + 1)">下一页</v-btn></div>
  </nav>
</template>
<style scoped>
.ima-pagination{display:flex;align-items:center;flex-wrap:wrap;gap:12px;padding:16px 22px;border-top:1px solid #e8edf5;font-size:12px;color:#667c9b}.ima-page-size{display:flex;align-items:center;gap:8px}.ima-page-size select{background:#fff;border:1px solid #d6dfec;border-radius:6px;padding:5px 8px;color:#344f76;cursor:pointer}.ima-page-size select:focus-visible{outline:2px solid #245bd6;outline-offset:2px}.ima-page-buttons{display:flex;align-items:center;gap:3px;margin-left:auto}.ima-page-buttons .v-btn{min-width:32px;padding:0 9px}@media(max-width:700px){.ima-pagination{padding:14px;gap:10px}.ima-page-count{flex:1}.ima-page-buttons{width:100%;justify-content:center;margin:0}.ima-page-buttons .v-btn{padding:0 7px}}
</style>
