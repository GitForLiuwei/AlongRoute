import { shallowRef } from 'vue'

export type ApiTrace = {
  id: number; method: string; path: string; startedAt: string
  durationMs: number | null; status: number | null; searchId: string | null
  release: string | null; logStatus: string | null; request: unknown; response: unknown; error: string
}
export const apiTraces = shallowRef<ApiTrace[]>([])
let sequence = 0
export function beginTrace(path: string, init: RequestInit): ApiTrace {
  const trace: ApiTrace = { id: ++sequence, method: init.method || 'GET', path,
    startedAt: new Date().toISOString(), durationMs: null, status: null, searchId: null,
    release: null, logStatus: null, request: typeof init.body === 'string' ? JSON.parse(init.body) : null,
    response: null, error: '' }
  apiTraces.value = [trace, ...apiTraces.value].slice(0, 30)
  return trace
}
export function finishTrace(trace: ApiTrace) {
  apiTraces.value = apiTraces.value.map(item => item.id === trace.id ? { ...trace } : item)
}
export function downloadDiagnostic(name: string, data: unknown) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url; link.download = name; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
