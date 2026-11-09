<script setup>
import { computed, ref } from 'vue'

const props = defineProps({ api: { type: Function, required: true }, tool: { type: String, default: '' }, label: { type: String, default: '清除日志' } })
const emit = defineEmits(['notice', 'cleared'])
const opened = ref(false), busy = ref(false), scope = ref('history'), target = ref(''), failure = ref('')
const title = computed(() => ({ sync:'FusionLive 同步', nas:'绿联云上传', wechat:'微信与 Codex', ima:'ima 知识库' })[target.value] || '全部工具及管理中心')
function open() { target.value = props.tool; scope.value = 'history'; failure.value = ''; opened.value = true }
async function clear() {
  if (busy.value) return
  busy.value = true; failure.value = ''
  try {
    const result = await props.api('/logs/clear', { method:'POST', body:JSON.stringify({ tool:target.value, scope:scope.value }) })
    let summary = `已清除 ${result.deleted} 个日志文件，释放 ${(result.freed_bytes / 1024 / 1024).toFixed(2)} MB；保留 ${result.kept} 个当日日志，跳过 ${result.skipped} 个运行中或受保护日志。`
    if (result.errors?.length) summary += ` ${result.errors.length} 个文件清理失败：` + result.errors.slice(0,3).map(item => `${item.source}：${item.reason}`).join('；')
    opened.value = false
    emit('notice', summary)
    emit('cleared', summary, result)
  } catch (error) { failure.value = error.message }
  finally { busy.value = false }
}
</script>

<template>
  <v-btn prepend-icon="mdi-delete-outline" variant="outlined" color="error" @click="open">{{ label }}</v-btn>
  <v-dialog v-model="opened" max-width="540" :persistent="busy">
    <v-card title="清除日志">
      <v-card-text>
        <p class="mb-4">清理工具：{{ title }}</p>
        <v-select v-model="scope" label="清理范围" :disabled="busy" :items="[{title:'仅清除今天之前的日志（默认）',value:'history'},{title:'清除全部日志，包含今天',value:'all'}]" />
        <p class="text-body-2 mt-3">按文件最后更新时间判断历史日志。仅删除日志文件，保留台账、调度状态、解析缓存、会话及发送记录。运行中工具的日志会跳过，可安全停止对应工具后再清理。</p>
        <p class="text-body-2 mt-3">清理范围不受搜索词、日志级别或分页影响。删除后无法恢复。</p>
        <v-alert v-if="failure" type="error" variant="tonal" class="mt-4">{{ failure }}</v-alert>
      </v-card-text>
      <v-card-actions><v-spacer /><v-btn :disabled="busy" @click="opened = false">取消</v-btn><v-btn color="error" variant="flat" :loading="busy" @click="clear">确认清除</v-btn></v-card-actions>
    </v-card>
  </v-dialog>
</template>
