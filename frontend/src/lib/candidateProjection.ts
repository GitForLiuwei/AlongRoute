import type { Point, Recommendation } from './api'
import { validGeoPoint } from './geoProjector'

export type CandidateEvent = {
  event: string; sequence?: number; candidate_key?: unknown
  point?: { name?: string; point?: Point }
  recommendation?: { id?: string; point?: { name?: string; point?: Point } }
  reason?: string
}
export type CandidateReplayResponse = { search_id: string; finished: boolean; events: CandidateEvent[] }
export type ProjectionCandidate = {
  key: string; point: Point; name: string
  status: 'pending' | 'accepted' | 'rejected' | 'recommended'
  reason: string; recommendationId?: string
}
const reasons: Record<string, string> = {
  detour_limit: '司机增加时间超出上限',
  unusable_or_unknown_road: '道路条件不适用或未知',
  missing_continuous_driver_route: '未取得连续司机路线',
  exit_not_reached_in_order: '未按顺序通过指定出口',
  no_viable_onward_options: '没有符合条件的后续交通方案',
  map_service_error: '地图服务查询失败',
  evaluation_cancelled_or_budget_exhausted: '核验取消或预算耗尽',
}

/** Only point-level rejection events may remove a point. A filtered transport
 * option, an untested candidate, or a missing record never proves rejection.
 */
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
      candidate = { key, point, name: `候选 ${point.lon.toFixed(5)}, ${point.lat.toFixed(5)}`, status: 'pending', reason: '未完成核验' }
      pool.set(key, candidate)
    }
    candidate.name = event.point?.name || event.recommendation?.point?.name || candidate.name
    if (event.event === 'candidate_rejected') {
      candidate.status = 'rejected'
      candidate.reason = reasons[event.reason || ''] || '未通过本次路线核验'
    } else if (event.event === 'candidate_accepted') {
      candidate.status = 'accepted'
      candidate.reason = '路线核验通过，未入选本次最终推荐'
      candidate.recommendationId = event.recommendation?.id
    } else if (event.event === 'candidate_not_evaluated') {
      candidate.status = 'pending'
      candidate.reason = '尚未完成核验，不能视为不可用'
    }
  }
  const recommended = new Set(final.map(r => r.id))
  for (const candidate of pool.values()) {
    if (candidate.recommendationId && recommended.has(candidate.recommendationId)) {
      candidate.status = 'recommended'
      candidate.reason = '最终推荐方案'
    }
  }
  return [...pool.values()]
}
