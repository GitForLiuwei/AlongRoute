// The browser uses Vite's same-origin proxy. The packaged iOS app connects
// directly to FastAPI; API, SSE and the map security proxy must share this base.
export function normalizeServiceBase(value: string): string {
  const base = value.trim().replace(/\/+$/, '')
  if (!base) return ''
  const url = new URL(base)
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password
    || url.search || url.hash || url.pathname !== '/') {
    throw new Error('VITE_API_BASE_URL 必须是 http(s) 服务根地址，例如 https://api.example.com')
  }
  return url.origin
}

const serviceBase = normalizeServiceBase(import.meta.env.VITE_API_BASE_URL || '')

export function serviceUrl(path: string): string {
  if (!path.startsWith('/') || path.startsWith('//')) throw new Error('服务路径必须以单个 / 开头')
  return `${serviceBase}${path}`
}

export function mapServiceHost(): string {
  return `${serviceBase || window.location.origin}/_AMapService`
}
