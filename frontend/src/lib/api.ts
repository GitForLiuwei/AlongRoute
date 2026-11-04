export type Point = { lon: number; lat: number }
export type Place = { name: string; point: Point; citycode?: string | null; poi_id?: string | null }
export type PlaceSuggestion = Place & { address: string }
export type Priority = 'balanced' | 'time' | 'cost'
export type Mode = 'taxi' | 'transit'
export type Preferences = {
  max_detour_minutes: number
  modes: Mode[]
  priority: Priority
  max_walking_m: number
  max_transfers: number
}
export type Route = { duration_s: number; distance_m: number; polyline: Point[]; fare_yuan: number | null }
export type TransitStep = {
  kind: 'walk' | 'bus' | 'subway'; title: string
  distance_m: number | null; duration_s: number | null
  from_stop: string | null; to_stop: string | null
  line_name: string | null; direction: string | null; stop_count: number | null
  via_stops: string[]; entrance: string | null; exit: string | null; instructions: string[]
}
export type ModeOption = {
  mode: Mode; duration_s: number; fare_yuan: number | null
  walking_m: number | null; transfers: number | null; details: string
  route: Point[]; notes: string[]
  transit_steps?: TransitStep[]
  total_duration_s: number | null; arrival_at: string | null
  boarding_walk_m: number | null; boarding_stop: Place | null
  boarding_kind: 'bus' | 'subway' | null; boarding_walk_route: Point[]
}
export type Recommendation = {
  id: string; point: Place; driver_detour_s: number; driver_route: Route
  shared_ride_s: number; stop_duration_s: number
  candidate_source: 'roadside' | 'parking' | 'station_area'
  options: ModeOption[]; suggested_mode: Mode; score: number
  driving_extra_s?: number; comparison?: string
  highway_exit?: Place | null; original_exit?: boolean
  stop_status: 'unverified' | 'verified'; stop_warning: string; reason: string
}
export type RecommendationResponse = {
  mode: 'demo' | 'live'; baseline_route: Route
  recommendations: Recommendation[]; warnings: string[]
  generated_at: string; examined_candidates: number
  comparisons?: { label: string; options: ModeOption[] }[]
  search_complete?: boolean; departure_at?: string | null; expires_at?: string | null
  station_walk_limit_m: 300
}
export type RecommendationRequest = {
  origin: Place; driver_destination: Place; passenger_destination: Place
  preferences: Preferences; departure_time?: string | null
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 115000)
  const abort = () => controller.abort()
  init.signal?.addEventListener('abort', abort, { once: true })
  try {
    const response = await fetch(path, { ...init, signal: controller.signal })
    const json = await response.json().catch(() => null)
    if (!response.ok) throw new Error(typeof json?.detail === 'string' ? json.detail : `服务暂不可用 (${response.status})`)
    if (json === null) throw new Error('服务返回了无法识别的数据，请重试')
    return json as T
  } catch (error) {
    if (controller.signal.aborted) throw new Error('计算已取消或超时，请重新计算')
    throw error
  } finally {
    clearTimeout(timer)
    init.signal?.removeEventListener('abort', abort)
  }
}
export function apiGet<T>(path: string): Promise<T> { return requestJson<T>(path) }
export function apiPost<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return requestJson<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal })
}
export function duration(s: number): string {
  const minutes = Math.ceil(s / 60)
  const h = Math.floor(minutes / 60), m = minutes % 60
  return h ? `${h}小时${m ? `${m}分` : ''}` : `${m} 分钟`
}

export function fare(v: number | null): string {
  return v === null ? '待估价' : `¥${Number(v).toFixed(v % 1 ? 1 : 0)}`
}

export function arrival(value: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Shanghai',
  }) : '待估算'
}
