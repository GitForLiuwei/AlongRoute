import type { Point, Recommendation } from './api'
import { validGeoPoint } from './geoProjector'

export type CandidateEvent = {
  event: string; sequence?: number; candidate_key?: unknown
  point?: { name?: string; point?: Point }
  recommendation?: { id?: string; point?: { name?: string; point?: Point }; driver_detour_s?: number }
  reason?: string
  driver_detour_s?: number
  driver_polyline?: Point[]
  passenger_polyline?: Point[]
}
export type CandidateReplayResponse = { search_id: string; finished: boolean; events: CandidateEvent[] }
export type ProjectionCandidate = {
  key: string; point: Point; name: string
  status: 'pending' | 'accepted' | 'rejected' | 'recommended'
  reason: string; recommendationId?: string
  driverDetourMin?: number
  driverPolyline?: Point[]
  passengerPolyline?: Point[]
}
const reasons: Record<string, string> = {
  detour_limit: '司机绕行超出时间上限',
  detour_bound_pruned: '绕行远超已优选方案',
  unusable_or_unknown_road: '高速/高架或禁停路段不可靠边',
  missing_continuous_driver_route: '未取得连续可通行司机路线',
  exit_not_reached_in_order: '未按行驶顺序经过指定高速口',
  no_viable_onward_options: '下车后无符合限制的接驳方案',
  map_service_error: '地图服务查询超时',
  evaluation_cancelled_or_budget_exhausted: '核验预算耗尽',
}

export function projectionCandidates(events: CandidateEvent[], final: Recommendation[]): ProjectionCandidate[] {
  const pool = new Map<string, ProjectionCandidate>()
  for (const event of events.filter(e => e && typeof e === 'object').sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0))) {
    if (!['candidate_generated', 'candidate_started', 'candidate_accepted', 'candidate_rejected', 'candidate_not_evaluated'].includes(event.event)) continue
    if (!Array.isArray(event.candidate_key) || typeof event.candidate_key[0] !== 'string') continue
    const key = JSON.stringify(event.candidate_key)
    const [lon, lat] = event.candidate_key[0].split(',').map(Number)
    const point = event.point?.point ?? event.recommendation?.point?.point ?? { lon, lat }
    if (!validGeoPoint(point)) continue
    let candidate = pool.get(key)
    if (!candidate) {
      candidate = { key, point, name: `候选 ${point.lon.toFixed(4)}, ${point.lat.toFixed(4)}`, status: 'pending', reason: '未完成核验' }
      pool.set(key, candidate)
    }
    candidate.name = event.point?.name || event.recommendation?.point?.name || candidate.name
    if (Array.isArray(event.driver_polyline) && event.driver_polyline.length >= 2) {
      candidate.driverPolyline = event.driver_polyline.filter(validGeoPoint)
    }
    if (Array.isArray(event.passenger_polyline) && event.passenger_polyline.length >= 2) {
      candidate.passengerPolyline = event.passenger_polyline.filter(validGeoPoint)
    }
    if (typeof event.driver_detour_s === 'number') {
      candidate.driverDetourMin = Math.round(event.driver_detour_s / 60)
    } else if (typeof event.recommendation?.driver_detour_s === 'number') {
      candidate.driverDetourMin = Math.round(event.recommendation.driver_detour_s / 60)
    }
    if (event.event === 'candidate_rejected') {
      candidate.status = 'rejected'
      const baseReason = reasons[event.reason || ''] || '未通过本次路网核验'
      candidate.reason = candidate.driverDetourMin != null ? `${baseReason} (+${candidate.driverDetourMin}分钟)` : baseReason
    } else if (event.event === 'candidate_accepted') {
      candidate.status = 'accepted'
      candidate.reason = candidate.driverDetourMin != null ? `核验通过 (顺路+${candidate.driverDetourMin}分钟)` : '路网核验通过'
      candidate.recommendationId = event.recommendation?.id
    } else if (event.event === 'candidate_not_evaluated') {
      candidate.status = 'pending'
      candidate.reason = '已提前收敛，未继续精算'
    }
  }
  const recommendedMap = new Map(final.map(r => [r.id, r]))
  for (const candidate of pool.values()) {
    if (candidate.recommendationId && recommendedMap.has(candidate.recommendationId)) {
      const rec = recommendedMap.get(candidate.recommendationId)!
      candidate.status = 'recommended'
      candidate.driverDetourMin = Math.round(rec.driver_detour_s / 60)
      candidate.reason = `★ 优选晋级 (司机+${candidate.driverDetourMin}分钟)`
      if (!candidate.driverPolyline?.length && rec.driver_route?.polyline?.length) {
        candidate.driverPolyline = rec.driver_route.polyline
      }
      const opt = rec.options.find(o => o.mode === rec.suggested_mode) || rec.options[0]
      if (!candidate.passengerPolyline?.length && opt?.route?.length) {
        candidate.passengerPolyline = opt.route
      }
    }
  }
  // 只把真正参与核验的候选点（accepted / rejected / recommended）优先放在前面回放，避免未核验的 pending 点拖慢动画
  return [...pool.values()].sort((a, b) => {
    const order = { rejected: 0, accepted: 1, recommended: 2, pending: 3 }
    return order[a.status] - order[b.status]
  })
}
