<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import AMapLoader from '@amap/amap-jsapi-loader'
import { MapPinned, Layers3 } from 'lucide-vue-next'
import type { Place, Point, Recommendation, RecommendationResponse } from '../lib/api'
import { validGeoPoint } from '../lib/geoProjector'
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
const liveMap = computed(() => !!jsKey && props.proxyReady && !mapError.value)
const mapLoaded = ref(false)
let map: any = null
let AMap: any = null
let initializing = false
let disposed = false
let lastFit = ''
const directWalk = computed(() => props.active?.options.find(x => x.mode === props.shownMode)?.direct_walk)
const transitOption = computed(() => props.shownMode === 'transit' ? props.active?.options.find(x => x.mode === 'transit') : null)

const diagnosticPoints = computed(() => props.candidates || [])
const inspectingCandidate = computed(() => diagnosticPoints.value.find(c => c.key === props.inspectKey))
const inspectingRecommendation = computed(() => props.result?.recommendations.find(r => r.id === inspectingCandidate.value?.recommendationId))
const inspectingDriverLine = computed<Point[]>(() => {
  const cand = inspectingCandidate.value
  if (!cand) return []
  if (cand.driverPolyline && cand.driverPolyline.length >= 2) return cand.driverPolyline
  if (inspectingRecommendation.value?.driver_route.polyline?.length) return inspectingRecommendation.value.driver_route.polyline
  if (props.origin && props.driverDestination) return [props.origin.point, cand.point, props.driverDestination.point]
  return []
})
const inspectingPassengerLine = computed<Point[]>(() => {
  const cand = inspectingCandidate.value
  if (!cand) return []
  if (cand.passengerPolyline && cand.passengerPolyline.length >= 2) return cand.passengerPolyline
  const opt = inspectingRecommendation.value?.options.find(o => o.mode === inspectingRecommendation.value?.suggested_mode)
  if (opt?.route?.length) return opt.route
  if (props.passengerDestination) return [cand.point, props.passengerDestination.point]
  return []
})
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
    let timeout: ReturnType<typeof setTimeout> | undefined
    try {
      AMap = await Promise.race([
        AMapLoader.load({ key: jsKey, version: '2.0' }),
        new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('地图加载超时')), 20000) }),
      ])
    } finally { clearTimeout(timeout) }
    if (!liveMap.value || disposed || !container.value) return
    map = new AMap.Map(container.value, {
      zoom: 10, center: [props.origin?.point.lon || 113.32, props.origin?.point.lat || 23.12],
      mapStyle: dark.value ? 'amap://styles/darkblue' : 'amap://styles/normal',
    })
    mapLoaded.value = true
    drawOnMap()
  } catch (e) {
    if (!disposed) mapError.value = e instanceof Error ? e.message : '高德地图加载失败'
  } finally { initializing = false }
}
function retryMap() {
  AMapLoader.reset()
  mapError.value = ''
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
    const inspecting = candidate.key === props.inspectKey
    overlays.push(new AMap.CircleMarker({ center: [candidate.point.lon, candidate.point.lat], radius: inspecting ? 11 : 7,
      fillColor: diagnosticColor(candidate), strokeColor: inspecting ? '#1e293b' : '#fff', strokeWeight: inspecting ? 3 : 2,
      fillOpacity: stage.value === 4 ? .3 : .8 }))
  }
  if (planner.value && stage.value === 4) for (const r of candidatePoints.value) {
    const marker = new AMap.Marker({ position: [r.point.point.lon, r.point.point.lat], title: r.point.name })
    marker.on('click', () => emit('pick', r))
    overlays.push(marker)
  }
  if (stage.value === 3 && inspectingCandidate.value) {
    const rejected = inspectingCandidate.value.status === 'rejected'
    draw(inspectingDriverLine.value, rejected ? '#dc2626' : '#2563eb', 6, rejected)
    draw(inspectingPassengerLine.value, rejected ? '#ef4444' : '#059669', 5, true)
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
    const fit = JSON.stringify([props.origin?.point, props.driverDestination?.point,
      props.passengerDestination?.point, props.result?.search_id])
    if (lastFit !== fit) {
      map.setFitView(undefined, false, planner.value ? [110, 70, 180, 70] : [70, 70, 70, 70])
      lastFit = fit
    }
  }
}
watch(liveMap, isLive => {
  if (isLive) void initMap()
  else if (map) { map.destroy(); map = null; mapLoaded.value = false; lastFit = '' }
}, { immediate: true })
watch(() => [props.result, props.active, props.shownMode, props.origin, props.driverDestination, props.passengerDestination, props.searchStage, props.candidates, props.visibleCandidateCount, props.inspectKey, props.removedKeys, props.showRemoved],
  () => drawOnMap())
onBeforeUnmount(() => { disposed = true; map?.destroy(); map = null })
</script>

<template>
  <section class="map-area" :class="{ 'dark-map': dark, 'bright-map': bright }">
    <div v-if="!planner" class="map-toolbar">
      <span><MapPinned :size="15" /> 路线总览</span>
      <span class="map-mode"><Layers3 :size="14" /> 高德底图</span>
    </div>
    <div v-if="liveMap" ref="container" class="map-real"></div>
    <div v-if="!mapLoaded" class="map-loading" role="status">
      <template v-if="mapError"><strong>高德底图加载失败</strong><p>请检查网络后重试。</p><button @click="retryMap">重新加载地图</button></template>
      <p v-else-if="!jsKey">尚未配置高德地图，请联系管理员。</p>
      <p v-else-if="!proxyReady">正在连接高德地图服务…</p>
      <p v-else>正在加载高德底图…</p>
    </div>
    <div class="map-legend">
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
.map-loading { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 100px 24px 160px; text-align: center; color: #475569; background: #f1f5f9; }
.map-loading p { margin: 8px 0; }
.map-loading button { padding: 9px 18px; border: 1px solid #cbd5e1; border-radius: 8px; color: #1d4ed8; background: white; }
.map-legend { flex-wrap: wrap; max-width: calc(100% - 20px); }
.dark-map .map-legend { left: 24px; right: auto; bottom: 110px; gap: 12px; background: #0f172add; border: 1px solid #243047; border-radius: 9px; box-shadow: none; padding: 9px 12px; }
.dark-map .map-legend span { color: #94a3b8; font-size: 9px; }
.bright-map .map-legend { left: 26px; right: auto; bottom: 123px; gap: 12px; background: #fffffff5; border: 2px solid #cbd5e1; border-radius: 12px; box-shadow: 0 3px 14px #0f172a08; padding: 12px 14px; }
.bright-map .map-legend span { color: #334155; font-size: 12px; font-weight: 800; }
@media(max-width:1300px) { .bright-map .map-legend { bottom: 170px; } }
@media(max-width:900px) { .bright-map .map-legend { left: 20px; bottom: 164px; gap: 10px; padding: 10px; } .bright-map .map-legend span { font-size: 11px; } }
@media(max-width:480px) { .bright-map .map-legend { left: 16px; gap: 7px 10px; padding: 9px; } .bright-map .map-legend span { font-size: 10px; } }
</style>
