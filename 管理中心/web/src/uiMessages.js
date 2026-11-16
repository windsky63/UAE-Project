import { ref } from 'vue'

export const messages = ref([])
export const messageHistory = ref([])
const timers = new Map()
let sequence = 0

export function dismissMessage(id) {
  clearTimeout(timers.get(id))
  timers.delete(id)
  messages.value = messages.value.filter(item => item.id !== id)
}

export function clearMessages() {
  for (const timer of timers.values()) clearTimeout(timer)
  timers.clear()
  messages.value = []
  messageHistory.value = []
}

export function publishMessage(text, error = false) {
  if (!text) return
  const type = error ? 'error' : /已提交|已启动|等待|正在|请求/.test(text) ? 'info' : 'success'
  const item = { id: ++sequence, text: String(text), type, time: new Date().toLocaleTimeString('zh-CN', { hour12: false }) }
  messageHistory.value = [item, ...messageHistory.value].slice(0, 80)
  messages.value = [...messages.value, item]
  if (type !== 'error') timers.set(item.id, setTimeout(() => dismissMessage(item.id), 6000))
}
