export type Point = { lon: number; lat: number }
export type Place = { name: string; point: Point; citycode?: string | null; poi_id?: string | null }
export type PlaceSuggestion = Place & { address: string }
export type Priority = 'balanced' | 'time' | 'cost'
export type Mode = 'taxi' | 'transit'
export type Preferences = {
  max_detour_minutes: number
  max_taxi_fare_yuan: number
  modes: Mode[]
  priority: Priority
  max_walking_m: number
  max_transfers: number
}
export type DrivingStep = {
  road_name: string; instruction: string; action: string; assistant_action: string
  distance_m: number | null; duration_s: number | null
}
export type Route = { duration_s: number; distance_m: number; polyline: Point[]; fare_yuan: number | null; driving_steps?: DrivingStep[] }
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
  recommendation_label?: string
  dropoff_road?: string | null
  highway_exit?: Place | null; original_exit?: boolean
  stop_status: 'unverified' | 'verified'; stop_warning: string; reason: string
}
export type RecommendationResponse = {
  search_id?: string | null
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
export type ManualDropoffResponse = {
  search_id: string | null
  origin: Place; driver_destination: Place; passenger_destination: Place; dropoff: Place
  baseline_route: Route; driver_route: Route; taxi_route: Route | null
  driving_extra_s: number; driver_detour_s: number; shared_ride_s: number
  stop_duration_s: number; wait_estimate_s: number
  fare_yuan: number | null; passenger_total_s: number | null; arrival_at: string | null
  combined_score: number | null
  max_detour_minutes: number; max_taxi_fare_yuan: number
  within_detour_limit: boolean; within_taxi_fare_limit: boolean | null; road_condition_usable: boolean
  warnings: string[]; generated_at: string; departure_at: string; expires_at: string
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 115000)
  const abort = () => controller.abort()
  init.signal?.addEventListener('abort', abort, { once: true })
  try {
    const response = await fetch(path, { ...init, signal: controller.signal })
    const json = await response.json().catch(() => null)
    if (!response.ok) {
      const message = typeof json?.detail === 'string' ? json.detail : `服务暂不可用 (${response.status})`
      const id = response.headers.get('X-Search-ID')
      throw new Error(id ? `${message}（搜索编号：${id}）` : message)
    }
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

export function distance(m: number): string {
  return m < 1000 ? `${Math.round(m)} 米` : `${(m / 1000).toFixed(1)} 公里`
}

export function coordinates(p: Point): string {
  return `${p.lon.toFixed(6)}, ${p.lat.toFixed(6)}`
}

export function detourDuration(s: number): string {
  const seconds = Math.max(0, Math.round(s))
  const minutes = Math.floor(seconds / 60), remainder = seconds % 60
  return minutes ? `${minutes}分${remainder ? `${remainder}秒` : ''}` : `${remainder}秒`
}

export function fare(v: number | null): string {
  return v === null ? '待估价' : `¥${Number(v).toFixed(v % 1 ? 1 : 0)}`
}

export function arrival(value: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Shanghai',
  }) : '待估算'
}
