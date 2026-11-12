<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import AMapLoader from '@amap/amap-jsapi-loader'
import { MapPinned, Layers3 } from 'lucide-vue-next'
import type { Place, Point, Recommendation, RecommendationResponse } from '../lib/api'
import { createGeoProjector, validGeoPoint } from '../lib/geoProjector'
import type { ProjectionCandidate } from '../lib/candidateProjection'

const props = defineProps<{
  result: RecommendationResponse | null
  active: Recommendation | null
  origin: Place | null
  driverDestination: Place | null
  passengerDestination: Place | null
  shownMode: 'taxi' | 'transit'
  proxyReady: boolean
  theme?: 'light' | 'dark' | 'bright'
  searchStage?: number
  projected?: boolean
  candidates?: ProjectionCandidate[]
  visibleCandidateCount?: number
  inspectKey?: string
  removedKeys?: string[]
  showRemoved?: boolean
}>()
const emit = defineEmits<{ pick: [r: Recommendation] }>()
const dark = computed(() => props.theme === 'dark')
const bright = computed(() => props.theme === 'bright')
const planner = computed(() => dark.value || bright.value)
const stage = computed(() => props.searchStage ?? 4)
const candidatePoints = computed(() => {
  const seen = new Set<string>()
  return (props.result?.recommendations || []).filter(r => {
    const key = `${r.point.point.lon},${r.point.point.lat}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
})
const container = ref<HTMLDivElement | null>(null)
const mapError = ref('')
const jsKey = import.meta.env.VITE_AMAP_JS_KEY || ''
const liveMap = computed(() => !props.projected && props.result?.mode === 'live' && !!jsKey && props.proxyReady && !mapError.value)
let map: any = null
let AMap: any = null
let initializing = false
let disposed = false
const directWalk = computed(() => props.active?.options.find(x => x.mode === props.shownMode)?.direct_walk)
const transitOption = computed(() => props.shownMode === 'transit' ? props.active?.options.find(x => x.mode === 'transit') : null)

// Fit every route and diagnostic point once per result, so selecting a card or
// removing candidates cannot move the remaining points or change their scale.
const allPoints = computed(() => [
  props.origin?.point, props.driverDestination?.point, props.passengerDestination?.point,
  ...(props.result?.baseline_route.polyline || []),
  ...(props.result?.recommendations.flatMap(r => [r.point.point, ...r.driver_route.polyline,
    ...r.options.flatMap(o => [...o.route, ...o.boarding_walk_route, ...(o.boarding_stop ? [o.boarding_stop.point] : [])])]) || []),
  ...(props.candidates || []).map(c => c.point),
].filter(validGeoPoint))
const projector = computed(() => createGeoProjector(allPoints.value))
function xy(p: Point): [number, number] { return projector.value(p) }
function plotted(points: Point[] | undefined): string {
  return (points || []).filter(validGeoPoint).map(p => xy(p).map(n => n.toFixed(2)).join(',')).join(' ')
}
const diagnosticPoints = computed(() => props.candidates || [])
const inspectingCandidate = computed(() => diagnosticPoints.value.find(c => c.key === props.inspectKey))
const inspectingRecommendation = computed(() => props.result?.recommendations.find(r => r.id === inspectingCandidate.value?.recommendationId))
const visibleDiagnostics = computed(() => {
  if (stage.value === 4) return props.showRemoved ? diagnosticPoints.value.filter(c => c.status === 'rejected') : []
  if (stage.value !== 2 && stage.value !== 3) return []
  return diagnosticPoints.value.slice(0, props.visibleCandidateCount ?? Infinity).filter(c => !(props.removedKeys || []).includes(c.key))
})
function inspected(c: ProjectionCandidate) {
  if (stage.value === 4) return true
  if (stage.value !== 3 || !inspectingCandidate.value) return false
  return diagnosticPoints.value.indexOf(c) <= diagnosticPoints.value.indexOf(inspectingCandidate.value)
}
function diagnosticColor(c: ProjectionCandidate) {
  if (!inspected(c)) return '#2563eb'
  return c.status === 'rejected' ? '#dc2626' : c.status === 'pending' ? '#94a3b8' : '#059669'
}

const markers = computed(() => [
  props.origin ? { label: 'A', name: '出发', p: props.origin.point, type: 'origin' } : null,
  props.driverDestination ? { label: 'B', name: '司机终点', p: props.driverDestination.point, type: 'driver' } : null,
  props.passengerDestination ? { label: 'C', name: '朋友终点', p: props.passengerDestination.point, type: 'passenger' } : null,
  props.active && (!planner.value || stage.value === 4) ? { label: 'P', name: props.active.point.name, p: props.active.point.point, type: 'dropoff' } : null,
  transitOption.value?.boarding_stop && (!planner.value || stage.value === 4) ? { label: 'S', name: transitOption.value.boarding_stop.name, p: transitOption.value.boarding_stop.point, type: 'dropoff' } : null,
].filter((p): p is { label: string; name: string; p: Point; type: string } => !!p))

async function initMap() {
  if (!liveMap.value || map || initializing || disposed) return
  initializing = true
  await nextTick()
  if (!container.value || !liveMap.value || disposed) { initializing = false; return }
  try {
    ;(window as any)._AMapSecurityConfig = {
      serviceHost: `${window.location.origin}/_AMapService`,
    }
    AMap = await AMapLoader.load({ key: jsKey, version: '2.0' })
    if (!liveMap.value || disposed || !container.value) return
    map = new AMap.Map(container.value, {
      zoom: 10, center: [props.origin?.point.lon || 113.32, props.origin?.point.lat || 23.12],
      mapStyle: dark.value ? 'amap://styles/darkblue' : 'amap://styles/normal',
    })
    drawOnMap()
  } catch (e) {
    if (!disposed) mapError.value = e instanceof Error ? e.message : '高德地图加载失败'
  } finally { initializing = false }
}
function drawOnMap() {
  if (!map || !AMap) return
  map.clearMap()
  const overlays: any[] = []
  const draw = (coords: Point[] | undefined, color: string, lineWidth: number, dashed = false) => {
    if (!coords || coords.length < 2) return
    const line = new AMap.Polyline({
      path: coords.map(p => [p.lon, p.lat]), strokeColor: color,
      strokeWeight: lineWidth, strokeOpacity: lineWidth > 10 ? .1 : .85,
      ...(dashed ? { strokeStyle: 'dashed' } : {}),
    })
    overlays.push(line)
  }
  if (planner.value && stage.value >= 2) draw(props.result?.baseline_route.polyline, '#3b82f6', 45)
  draw(props.result?.baseline_route.polyline, '#9bb9df', 4)
  if (!planner.value || stage.value === 4) draw(props.active?.driver_route.polyline, '#2962db', 6)
  if (!planner.value || stage.value === 4) draw(props.active?.options.find(x => x.mode === props.shownMode)?.route,
    directWalk.value ? '#8a5bbb' : props.shownMode === 'taxi' ? '#ee9953' : '#209a74', 5, true)
  draw(transitOption.value?.boarding_walk_route, '#8a5bbb', 5, true)
  for (const candidate of visibleDiagnostics.value) {
    overlays.push(new AMap.CircleMarker({ center: [candidate.point.lon, candidate.point.lat], radius: 7,
      fillColor: diagnosticColor(candidate), strokeColor: '#fff', strokeWeight: 2,
      fillOpacity: stage.value === 4 ? .3 : .8 }))
  }
  if (planner.value && stage.value === 4) for (const r of candidatePoints.value) {
    const marker = new AMap.Marker({ position: [r.point.point.lon, r.point.point.lat], title: r.point.name })
    marker.on('click', () => emit('pick', r))
    overlays.push(marker)
  }
  if (stage.value === 3 && inspectingRecommendation.value) {
    draw(inspectingRecommendation.value.driver_route.polyline, '#2563eb', 6)
    draw(inspectingRecommendation.value.options.find(o => o.mode === inspectingRecommendation.value?.suggested_mode)?.route, '#059669', 5, true)
  }
  for (const item of markers.value) {
    const maker = new AMap.Marker({
      position: [item.p.lon, item.p.lat],
      label: { content: item.label, direction: 'top' }, title: item.name,
    })
    overlays.push(maker)
  }
  if (overlays.length) {
    map.add(overlays)
    map.setFitView(undefined, false, [70, 70, 70, 70])
  }
}
watch(liveMap, isLive => {
  if (isLive) void initMap()
  else if (map) { map.destroy(); map = null }
}, { immediate: true })
watch(() => [props.result, props.active, props.shownMode, props.origin, props.driverDestination, props.passengerDestination, props.searchStage, props.candidates, props.visibleCandidateCount, props.inspectKey, props.removedKeys, props.showRemoved],
  () => drawOnMap())
onBeforeUnmount(() => { disposed = true; map?.destroy(); map = null })
</script>

<template>
  <section class="map-area" :class="{ 'dark-map': dark, 'bright-map': bright }">
    <div v-if="!planner" class="map-toolbar">
      <span><MapPinned :size="15" /> 路线总览</span>
      <span class="map-mode"><Layers3 :size="14" /> {{ liveMap ? '真实地图' : '示意地图' }}</span>
    </div>
    <div v-if="liveMap" ref="container" class="map-real"></div>
    <svg v-else class="map-schematic" viewBox="0 0 800 580" preserveAspectRatio="xMidYMid meet" :aria-label="result?.mode === 'live' ? '真实经纬度等比例投影，非道路地图' : '经纬度投影示意图，演示结果不可用于导航'">
      <defs>
        <pattern :id="planner ? 'smart-city-grid' : 'city-grid'" width="60" height="60" patternUnits="userSpaceOnUse">
          <path d="M 60 0 L 0 0 0 60" fill="none" :stroke="dark ? '#33415544' : '#94a3b82b'" stroke-width="1" />
        </pattern>
        <pattern v-if="!planner" id="streets" width="128" height="93" patternUnits="userSpaceOnUse">
          <path d="M -20 34 L 148 34 M 40 -20 L 40 113" fill="none" stroke="#fff" stroke-width="12" />
          <path d="M -20 34 L 148 34 M 40 -20 L 40 113" fill="none" stroke="#e2e8ef" stroke-width="1" />
        </pattern>
      </defs>
      <rect width="800" height="580" :fill="planner ? 'transparent' : '#edf4f3'" />
      <rect width="800" height="580" :fill="planner ? 'url(#smart-city-grid)' : 'url(#city-grid)'" />
      <rect v-if="!planner" width="800" height="580" fill="url(#streets)" />
      <path v-if="!planner" d="M20 520 Q190 470 275 290 T760 45" fill="none" stroke="#dce9d8" stroke-width="94" />
      <path v-if="!planner" d="M20 520 Q190 470 275 290 T760 45" fill="none" stroke="#f8fbf7" stroke-width="30" />
      <polyline v-if="planner && !result && origin && driverDestination" :points="plotted([origin.point, driverDestination.point])" fill="none" stroke="#3b82f6" stroke-width="2" stroke-dasharray="5 9" opacity=".4" />
      <polyline v-if="planner && result && stage >= 2" :points="plotted(result.baseline_route.polyline)" fill="none" stroke="#3b82f6" stroke-width="60" opacity=".1" stroke-linecap="round" stroke-linejoin="round" />
      <g v-if="planner && stage === 3 && inspectingRecommendation" class="inspection-routes">
        <polyline :points="plotted(inspectingRecommendation.driver_route.polyline)" fill="none" stroke="#2563eb" stroke-width="7" opacity=".75" />
        <polyline :points="plotted(inspectingRecommendation.options.find(o => o.mode === inspectingRecommendation?.suggested_mode)?.route)" fill="none" stroke="#059669" stroke-width="5" stroke-dasharray="8 6" class="route-flow" />
      </g>
      <polyline :class="{ 'route-reveal': planner && stage === 1 }" pathLength="1000" v-if="result" :points="plotted(result.baseline_route.polyline)" fill="none"
        :stroke="planner ? '#2563eb' : '#a2bedf'" stroke-width="10" stroke-linejoin="round" stroke-linecap="round" />
      <polyline v-if="bright && result && stage >= 2" :points="plotted(result.baseline_route.polyline)" fill="none" stroke="white" stroke-width="3" stroke-dasharray="10 14" class="baseline-flow" stroke-linecap="round" />
      <polyline v-if="active && (!planner || stage === 4)" :points="plotted(active.driver_route.polyline)" fill="none"
        stroke="#2769dc" stroke-width="6" stroke-linejoin="round" stroke-linecap="round" />
      <polyline class="route-flow" v-if="active && (!planner || stage === 4)" :points="plotted(active.options.find(x => x.mode === shownMode)?.route)" fill="none"
        :stroke="directWalk ? '#8a5bbb' : shownMode === 'taxi' ? '#e89b48' : dark ? '#34d399' : '#209a74'" stroke-dasharray="12 9" stroke-width="5"
        stroke-linejoin="round" stroke-linecap="round" />
      <polyline v-if="transitOption" :points="plotted(transitOption.boarding_walk_route)" fill="none" stroke="#8a5bbb" stroke-dasharray="5 5" stroke-width="5" />
      <TransitionGroup v-if="planner" name="projection-dot" tag="g" class="projection-candidates">
        <g v-for="(candidate, index) in visibleDiagnostics" :key="candidate.key" class="projection-candidate" :data-status="candidate.status" :transform="`translate(${xy(candidate.point).join(',')})`">
          <title>{{ candidate.name }} · {{ inspected(candidate) ? candidate.reason : '初筛候选' }}</title>
          <g class="diagnostic-symbol" :class="{ ghost: stage === 4, 'inspecting-symbol': candidate.key === inspectKey }">
            <circle r="9" :fill="inspected(candidate) && candidate.status === 'rejected' ? '#fee2e2' : diagnosticColor(candidate)" :stroke="diagnosticColor(candidate)" stroke-width="2" />
            <path v-if="inspected(candidate) && candidate.status === 'rejected'" d="M-3-3L3 3 M-3 3L3-3" fill="none" stroke="#b91c1c" stroke-width="2" />
          </g>
          <text v-if="(candidate.key === inspectKey || stage === 4) && candidate.status === 'rejected'" class="projection-reason" x="13" y="-12">{{ candidate.reason }}</text>
        </g>
      </TransitionGroup>
      <g v-if="planner && stage === 4" class="map-candidates">
        <g v-for="(r, index) in candidatePoints" :key="r.id" class="map-candidate" :transform="`translate(${xy(r.point.point)[0]}, ${xy(r.point.point)[1]})`" tabindex="0" role="button" :aria-label="`选择${r.point.name}`" @click="emit('pick', r)" @keydown.enter="emit('pick', r)" @keydown.space.prevent="emit('pick', r)">
          <title>{{ r.point.name }}</title>
          <circle v-if="active?.id === r.id" r="25" fill="#10b98122" stroke="#34d399" stroke-width="1" class="candidate-pulse" />
          <circle :r="bright ? 15 : 11" :fill="bright ? '#059669' : '#34d399'" :stroke="bright ? 'white' : '#0f172a'" stroke-width="3" />
          <text text-anchor="middle" dy="4" :fill="bright ? 'white' : '#022c22'" :font-size="bright ? 13 : 10" font-weight="700">{{ (result?.recommendations.indexOf(r) ?? index) + 1 }}</text>
        </g>
      </g>
      <g class="map-place-marker" v-for="m in markers" :key="m.label" :transform="`translate(${xy(m.p)[0]}, ${xy(m.p)[1]})`">
        <circle v-if="!planner || m.label !== 'P'" r="18" :stroke="dark ? '#0f172a' : 'white'" stroke-width="5" :fill="m.type === 'dropoff' ? '#159c7b' : m.type === 'passenger' ? '#e89b48' : '#2865cb'" />
        <text class="map-place-letter" v-if="!planner || m.label !== 'P'" text-anchor="middle" dy="5" fill="white" font-size="15" font-weight="700">{{ m.label }}</text>
        <text class="map-label" text-anchor="middle" y="-28">{{ m.name }}</text>
      </g>
      <text v-if="!planner" x="28" y="550" fill="#82919e" font-size="15">路线示意，不作为真实道路或上下客位置依据</text>
      <text v-if="bright && stage === 1 && result" x="400" y="60" text-anchor="middle" fill="#1d4ed8" font-size="18" font-weight="900">正在锁定司机主干路线</text>
    </svg>
    <div v-if="mapError" class="map-issue">地图加载失败：{{ mapError }}，已切换为示意图</div>
    <div class="map-legend">
      <span v-if="planner && !liveMap">经纬度投影</span>
      <span v-if="planner && candidates?.some(c => c.status === 'rejected') && (stage === 3 || showRemoved)"><i style="background:#dc2626;width:8px;height:8px;border-radius:50%"></i>核验未通过</span>
      <span><i class="line-icon" style="border-color:#9bb9df"></i>司机原路线</span>
      <span><i class="line-icon driver-line"></i>司机经停路线</span>
      <span><i class="line-icon taxi-line"></i>打车</span>
      <span><i class="line-icon transit-line"></i>公交/地铁</span>
      <span v-if="directWalk"><i class="line-icon" style="border-color:#8a5bbb"></i>直接步行回家</span>
      <span v-else-if="transitOption"><i class="line-icon" style="border-color:#8a5bbb"></i>到站步行 · S 上车站</span>
    </div>
  </section>
</template>

<style scoped>
.dark-map .route-flow { animation: route-flow 4s linear infinite; }
@keyframes route-flow { to { stroke-dashoffset: -84; } }
.dark-map .map-schematic { padding: 75px 15px 160px; }
.dark-map .map-label { fill: #a7b8d1; stroke: #071020; stroke-width: 6px; font-size: 11px; }
.dark-map .map-legend { left: 24px; right: auto; bottom: 110px; gap: 12px; background: #0f172add; border: 1px solid #243047; border-radius: 9px; box-shadow: none; padding: 9px 12px; }
.dark-map .map-legend span { color: #94a3b8; font-size: 9px; }
.dark-map .map-issue { background: #78350f88; color: #fcd34d; bottom: 190px; max-width: calc(100% - 40px); }
.map-candidate { cursor: pointer; outline: none; }
.map-candidate:focus-visible > circle { stroke: #fff; stroke-width: 3px; }
.candidate-pulse { animation: candidate-pulse 1.8s infinite; transform-box: fill-box; transform-origin: center; }
@keyframes candidate-pulse { 50% { opacity: .3; transform: scale(1.25); } }
@media(max-width:760px) { .dark-map .map-schematic { padding: 90px 8px 150px; } .dark-map .map-legend { left: 16px; gap: 8px; padding: 8px; } .dark-map .map-legend span { font-size: 8px; } .dark-map .map-label { font-size: 20px; } .dark-map .map-place-letter { font-size: 23px; } .dark-map .map-place-marker > circle { r: 23px; } .dark-map .map-candidate > circle:not(.candidate-pulse) { r: 17px; } .dark-map .map-candidate > text { font-size: 18px; } }
@media(prefers-reduced-motion:reduce) { .candidate-pulse,.dark-map .route-flow { animation: none; } }
.map-legend { flex-wrap: wrap; max-width: calc(100% - 20px); }
.bright-map .map-schematic { padding: 115px 15px 195px; }
.bright-map .route-flow { animation: route-flow 3s linear infinite; }
.bright-map .baseline-flow { animation: route-flow 3.2s linear infinite; }
.bright-map .route-reveal { stroke-dasharray: 1000; animation: route-reveal 1s ease-out both; }
@keyframes route-reveal { from { stroke-dashoffset: 1000; } to { stroke-dashoffset: 0; } }
.bright-map .map-label { fill: #1e293b; stroke: #fff; stroke-width: 6px; font-size: 15px; font-weight: 900; }
.bright-map .map-legend { left: 26px; right: auto; bottom: 123px; gap: 12px; background: #fffffff5; border: 2px solid #cbd5e1; border-radius: 12px; box-shadow: 0 3px 14px #0f172a08; padding: 12px 14px; }
.bright-map .map-legend span { color: #334155; font-size: 12px; font-weight: 800; }
.bright-map .map-issue { background: #fffbeb; border: 1px solid #fcd34d; color: #92400e; bottom: 250px; max-width: calc(100% - 40px); font-size: 13px; line-height: 1.8; }
.bright-map .map-candidate:focus-visible > circle { stroke: #1d4ed8; stroke-width: 4px; }
@media(max-width:1300px) { .bright-map .map-legend { bottom: 170px; } .bright-map .map-schematic { padding-top: 145px; padding-bottom: 235px; } }
@media(max-width:900px) {
  .bright-map .map-schematic { padding: 145px 10px 230px; }
  .bright-map .map-legend { left: 20px; bottom: 164px; gap: 10px; padding: 10px; }
  .bright-map .map-legend span { font-size: 11px; }
  .bright-map .map-label { font-size: 25px; }
  .bright-map .map-place-letter { font-size: 24px; }
  .bright-map .map-place-marker > circle { r: 24px; }
  .bright-map .map-candidate > circle:not(.candidate-pulse) { r: 20px; }
  .bright-map .map-candidate > text { font-size: 23px; }
}
@media(max-width:480px) {
  .bright-map .map-legend { left: 16px; gap: 7px 10px; padding: 9px; }
  .bright-map .map-legend span { font-size: 10px; }
  .bright-map .map-schematic { padding-top: 125px; }
}
@media(prefers-reduced-motion:reduce) { .bright-map .route-flow, .bright-map .baseline-flow, .bright-map .route-reveal { animation: none; } }
.projection-reason { fill: #b91c1c; font-size: 12px; font-weight: 800; stroke: #fff; stroke-width: 4px; paint-order: stroke; }
.projection-dot-enter-active, .projection-dot-leave-active { transition: opacity .35s ease; }
.projection-dot-enter-from, .projection-dot-leave-to { opacity: 0; }
.projection-dot-leave-active .diagnostic-symbol { transform-box: fill-box; transform-origin: center; animation: candidate-remove .32s ease-in forwards; }
@keyframes candidate-remove { 0%, 30% { opacity: 1; transform: scale(1.2); } 100% { opacity: 0; transform: scale(.15); } }
@media(prefers-reduced-motion:reduce) { .projection-dot-leave-active .diagnostic-symbol { animation: none; } .projection-dot-enter-active, .projection-dot-leave-active { transition: none; } }
.diagnostic-symbol.ghost { opacity: .5; }
.inspecting-symbol > circle { stroke-width: 4px; }
@media(max-width:900px) { .projection-candidate circle { r: 14px; } .projection-reason { font-size: 19px; } }
</style>
