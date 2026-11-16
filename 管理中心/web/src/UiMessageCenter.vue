<script setup>
import { ref } from 'vue'
import ActionIcon from './ActionIcon.vue'
import { messages, messageHistory, dismissMessage, clearMessages } from './uiMessages'
const opened = ref(false)
const names = { error: '失败', success: '成功', info: '提示' }
</script>

<template>
  <ActionIcon :label="`查看本次消息（${messageHistory.length}）`" icon="mdi-bell-outline" variant="text" @click="opened = true"/>
  <Teleport to="body">
    <div class="ui-message-stack" aria-label="操作消息" aria-live="polite" aria-relevant="additions">
      <v-alert v-for="item in messages" :key="item.id" :type="item.type" variant="tonal" class="ui-message" role="status">
        <span class="ui-message-text">{{ item.text }}</span>
        <template #close><ActionIcon :label="'关闭消息 ' + item.time" icon="mdi-close" variant="text" color="secondary" @click="dismissMessage(item.id)"/></template>
      </v-alert>
    </div>
  </Teleport>
  <v-dialog v-model="opened" max-width="680" scrollable>
    <v-card title="本次会话消息">
      <v-card-text class="message-history">
        <p class="text-body-2 text-secondary mb-4">保留最近 80 条；刷新页面后清空。任务结果请查看操作记录。</p>
        <div v-for="item in messageHistory" :key="item.id" class="message-history-row">
          <v-chip :color="item.type" size="small">{{ names[item.type] }}</v-chip><span>{{ item.text }}</span><time>{{ item.time }}</time>
        </div>
        <p v-if="!messageHistory.length" class="empty-state">本次会话暂无消息</p>
      </v-card-text>
      <v-card-actions><ActionIcon label="清空本次消息" icon="mdi-delete-outline" color="secondary" :disabled="!messageHistory.length" @click="clearMessages"/><v-spacer/><v-btn @click="opened = false">关闭</v-btn></v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style>
.ui-message-stack{position:fixed;top:76px;left:50%;transform:translateX(-50%);width:min(620px,calc(100vw - 32px));max-height:calc(100dvh - 100px);overflow:auto;z-index:2400;pointer-events:none;display:grid;gap:10px}
.ui-message-stack .ui-message{pointer-events:auto;background:rgb(var(--v-theme-surface) / .88);backdrop-filter:blur(6px);box-shadow:0 6px 24px #172b4d18;border:1px solid currentColor;border-radius:12px;grid-template-columns:max-content minmax(0,1fr) max-content max-content}
.ui-message-text{overflow-wrap:anywhere}
.ui-message .v-alert__content{min-width:0}
.ui-message .v-alert__close{align-self:start}
.message-history{max-height:65dvh}.message-history-row{display:flex;align-items:flex-start;gap:12px;padding:14px 0;border-bottom:1px solid #e8edf5}.message-history-row>span:not(.v-chip){flex:1;min-width:0;overflow-wrap:anywhere}.message-history-row time{font-size:12px;color:#66778e;white-space:nowrap}
@media(max-width:600px){.message-history-row{flex-wrap:wrap}.message-history-row time{margin-left:auto}}
</style>
