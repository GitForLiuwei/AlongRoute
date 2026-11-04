<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import AMapLoader from '@amap/amap-jsapi-loader'
import { MapPinned, Layers3 } from 'lucide-vue-next'
import type { Place, Point, Recommendation, RecommendationResponse } from '../lib/api'

const props = defineProps<{
  result: RecommendationResponse | null
  active: Recommendation | null
  origin: Place | null
  driverDestination: Place | null
  passengerDestination: Place | null
  shownMode: 'taxi' | 'transit'
  proxyReady: boolean
}>()
const container = ref<HTMLDivElement | null>(null)
const mapError = ref('')
const jsKey = import.meta.env.VITE_AMAP_JS_KEY || ''
const liveMap = computed(() => props.result?.mode === 'live' && !!jsKey && props.proxyReady && !mapError.value)
let map: any = null
let AMap: any = null
const transitOption = computed(() => props.shownMode === 'transit' ? props.active?.options.find(x => x.mode === 'transit') : null)

const allPoints = computed(() => [
  props.origin?.point, props.driverDestination?.point, props.passengerDestination?.point,
  ...(props.result?.baseline_route.polyline || []),
  ...(props.active?.driver_route.polyline || []),
  ...(props.active?.options.find(x => x.mode === props.shownMode)?.route || []),
  ...(transitOption.value?.boarding_walk_route || []),
].filter((p): p is Point => !!p))
const bounds = computed(() => {
  const points = allPoints.value.length ? allPoints.value : [{ lon: 113.2, lat: 23.2 }, { lon: 113.4, lat: 23.0 }]
  const lon = points.map(x => x.lon)
  const lat = points.map(x => x.lat)
  const minLon = Math.min(...lon), maxLon = Math.max(...lon)
  const minLat = Math.min(...lat), maxLat = Math.max(...lat)
  return { minLon, minLat, dx: Math.max(.005, maxLon - minLon), dy: Math.max(.005, maxLat - minLat) }
})
function xy(p: Point): [number, number] {
  const b = bounds.value
  return [80 + (p.lon - b.minLon) / b.dx * 640, 525 - (p.lat - b.minLat) / b.dy * 450]
}
function plotted(points: Point[] | undefined): string {
  return (points || []).map(p => xy(p).map(n => n.toFixed(1)).join(',')).join(' ')
}
const markers = computed(() => [
  props.origin ? { label: 'A', name: '出发', p: props.origin.point, type: 'origin' } : null,
  props.driverDestination ? { label: 'B', name: '司机终点', p: props.driverDestination.point, type: 'driver' } : null,
  props.passengerDestination ? { label: 'C', name: '朋友终点', p: props.passengerDestination.point, type: 'passenger' } : null,
  props.active ? { label: 'P', name: '待核实候选点', p: props.active.point.point, type: 'dropoff' } : null,
  transitOption.value?.boarding_stop ? { label: 'S', name: transitOption.value.boarding_stop.name, p: transitOption.value.boarding_stop.point, type: 'dropoff' } : null,
].filter((p): p is { label: string; name: string; p: Point; type: string } => !!p))

async function initMap() {
  if (!liveMap.value || map) return
  await nextTick()
  if (!container.value) return
  try {
    ;(window as any)._AMapSecurityConfig = {
      serviceHost: `${window.location.origin}/_AMapService`,
    }
    AMap = await AMapLoader.load({ key: jsKey, version: '2.0' })
    map = new AMap.Map(container.value, {
      zoom: 10, center: [props.origin?.point.lon || 113.32, props.origin?.point.lat || 23.12],
      mapStyle: 'amap://styles/normal',
    })
    drawOnMap()
  } catch (e) {
    mapError.value = e instanceof Error ? e.message : '高德地图加载失败'
  }
}
function drawOnMap() {
  if (!map || !AMap) return
  map.clearMap()
  const overlays: any[] = []
  const draw = (coords: Point[] | undefined, color: string, lineWidth: number, dashed = false) => {
    if (!coords || coords.length < 2) return
    const line = new AMap.Polyline({
      path: coords.map(p => [p.lon, p.lat]), strokeColor: color,
      strokeWeight: lineWidth, strokeOpacity: .85,
      ...(dashed ? { strokeStyle: 'dashed' } : {}),
    })
    overlays.push(line)
  }
  draw(props.result?.baseline_route.polyline, '#9bb9df', 4)
  draw(props.active?.driver_route.polyline, '#2962db', 6)
  draw(props.active?.options.find(x => x.mode === props.shownMode)?.route,
    props.shownMode === 'taxi' ? '#ee9953' : '#209a74', 5, true)
  draw(transitOption.value?.boarding_walk_route, '#8a5bbb', 5, true)
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
watch(() => [props.result, props.active, props.shownMode, props.origin, props.driverDestination, props.passengerDestination],
  () => drawOnMap())
onBeforeUnmount(() => { map?.destroy(); map = null })
</script>

<template>
  <section class="map-area">
    <div class="map-toolbar">
      <span><MapPinned :size="15" /> 路线总览</span>
      <span class="map-mode"><Layers3 :size="14" /> {{ liveMap ? '真实地图' : '示意地图' }}</span>
    </div>
    <div v-if="liveMap" ref="container" class="map-real"></div>
    <svg v-else class="map-schematic" viewBox="0 0 800 580" preserveAspectRatio="xMidYMid meet" aria-label="路线示意图，仅供演示">
      <defs>
        <pattern id="city-grid" width="60" height="60" patternUnits="userSpaceOnUse">
          <path d="M 60 0 L 0 0 0 60" fill="none" stroke="#dce5e7" stroke-width="1" />
        </pattern>
        <pattern id="streets" width="128" height="93" patternUnits="userSpaceOnUse">
          <path d="M -20 34 L 148 34 M 40 -20 L 40 113" fill="none" stroke="#fff" stroke-width="12" />
          <path d="M -20 34 L 148 34 M 40 -20 L 40 113" fill="none" stroke="#e2e8ef" stroke-width="1" />
        </pattern>
      </defs>
      <rect width="800" height="580" fill="#edf4f3" />
      <rect width="800" height="580" fill="url(#city-grid)" />
      <rect width="800" height="580" fill="url(#streets)" />
      <path d="M20 520 Q190 470 275 290 T760 45" fill="none" stroke="#dce9d8" stroke-width="94" />
      <path d="M20 520 Q190 470 275 290 T760 45" fill="none" stroke="#f8fbf7" stroke-width="30" />
      <polyline v-if="result" :points="plotted(result.baseline_route.polyline)" fill="none"
        stroke="#a2bedf" stroke-width="10" stroke-linejoin="round" stroke-linecap="round" />
      <polyline v-if="active" :points="plotted(active.driver_route.polyline)" fill="none"
        stroke="#2769dc" stroke-width="6" stroke-linejoin="round" stroke-linecap="round" />
      <polyline v-if="active" :points="plotted(active.options.find(x => x.mode === shownMode)?.route)" fill="none"
        :stroke="shownMode === 'taxi' ? '#e89b48' : '#209a74'" stroke-dasharray="12 9" stroke-width="5"
        stroke-linejoin="round" stroke-linecap="round" />
      <polyline v-if="transitOption" :points="plotted(transitOption.boarding_walk_route)" fill="none" stroke="#8a5bbb" stroke-dasharray="5 5" stroke-width="5" />
      <g v-for="m in markers" :key="m.label" :transform="`translate(${xy(m.p)[0]}, ${xy(m.p)[1]})`">
        <circle r="18" stroke="white" stroke-width="5" :fill="m.type === 'dropoff' ? '#159c7b' : m.type === 'passenger' ? '#e89b48' : '#2865cb'" />
        <text text-anchor="middle" dy="5" fill="white" font-size="15" font-weight="700">{{ m.label }}</text>
        <text class="map-label" text-anchor="middle" y="-28">{{ m.name }}</text>
      </g>
      <text x="28" y="550" fill="#82919e" font-size="15">路线示意，不作为真实道路或上下客位置依据</text>
    </svg>
    <div v-if="mapError" class="map-issue">地图加载失败：{{ mapError }}，已切换为示意图</div>
    <div class="map-legend">
      <span><i class="line-icon driver-line"></i>司机路线</span>
      <span><i class="line-icon taxi-line"></i>打车</span>
      <span><i class="line-icon transit-line"></i>公交/地铁</span>
      <span v-if="transitOption"><i class="line-icon" style="border-color:#8a5bbb"></i>到站步行 · S 上车站</span>
    </div>
  </section>
</template>
