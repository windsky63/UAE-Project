// randomUUID and Clipboard API can be unavailable on LAN HTTP origins.
export function requestId() {
  if (globalThis.crypto.randomUUID) return globalThis.crypto.randomUUID()
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`
}

export async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return } catch { /* Try selection copy. */ }
  }
  const focused = document.activeElement
  const field = document.createElement('textarea')
  field.value = text
  field.setAttribute('aria-label', '复制内容')
  field.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none'
  document.body.append(field)
  try {
    field.select()
    if (!document.execCommand('copy')) throw new Error('复制不可用')
  } finally {
    field.remove()
    focused?.focus({ preventScroll: true })
  }
}
