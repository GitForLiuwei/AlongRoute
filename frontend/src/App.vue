<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, watch, ref } from 'vue'
import { ArrowRight, BadgeHelp, BusFront, CarFront, Check, ChevronRight, CircleAlert, Clock3,
  Compass, ExternalLink, GitBranch, Info, LoaderCircle, MapPin, Menu, RefreshCcw, Route,
  ShieldAlert, SlidersHorizontal, Sparkles, Wallet, X } from 'lucide-vue-next'
import { wgs84ToGcj02 } from './lib/coordinates'
import PlaceField from './components/PlaceField.vue'
import SmartPlanner from './components/SmartPlanner.vue'
import MapCanvas from './components/MapCanvas.vue'
import TransitItinerary from './components/TransitItinerary.vue'
import DriverItinerary from './components/DriverItinerary.vue'
import ManualDropoffPanel from './components/ManualDropoffPanel.vue'
import CandidateReplay from './components/CandidateReplay.vue'
import DebugPanel from './components/DebugPanel.vue'
import { apiGet, apiPost, duration, detourDuration, distance, coordinates, fare, arrival, type Place, type Priority, type Mode,
  type Recommendation, type RecommendationResponse, type RecommendationRequest } from './lib/api'

function debugPageFromLocation() {
  if (window.location.hash === '#/debug') return true
  if (window.location.hash === '#/user') return false
  return window.location.pathname.replace(/\/$/, '') === '/debug'
}
const isDebug = ref(debugPageFromLocation())
const advancedOpen = ref(false)
const routeDetailsOpen = ref(false)
function syncPage() {
  isDebug.value = debugPageFromLocation()
  document.title = isDebug.value ? '开发调试 · 顺路达' : '顺路达 AlongRoute · 顺路下车点推荐'
  if (!isDebug.value) manualMode.value = false
}
function navigatePage(event: MouseEvent, path: '/user' | '/debug') {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  event.preventDefault()
  if (window.location.pathname !== path || window.location.hash) history.pushState(null, '', path)
  syncPage()
}
onMounted(() => {
  syncPage()
  window.addEventListener('hashchange', syncPage)
  window.addEventListener('popstate', syncPage)
})
onBeforeUnmount(() => {
  window.removeEventListener('hashchange', syncPage)
  window.removeEventListener('popstate', syncPage)
})
const origin = ref<Place | null>({ name: '乐从中学', point: { lon: 113.105351, lat: 22.940194 }, citycode: '0757', poi_id: 'B02F5076M7' })
const driverDestination = ref<Place | null>({ name: '惠阳区新圩镇东风村卫生站', point: { lon: 114.306364, lat: 22.825135 }, citycode: '0752', poi_id: 'B02F00QI66' })
const passengerDestination = ref<Place | null>({ name: '里城玺樾山', point: { lon: 114.122363, lat: 22.627733 }, citycode: '0755', poi_id: 'B0FFJ3JUUX' })
const detour = ref(120)
const manualMode = ref(false)
const taxiFareLimit = ref(200)
const selectedModes = ref<Mode[]>(['taxi', 'transit'])
const priority = ref<Priority>('balanced')
const walkLimit = ref(3000)
const transfers = ref(4)
const departure = ref('')
const mode = ref<'demo' | 'live'>('demo')
const proxyReady = ref(false)
const result = ref<RecommendationResponse | null>(null)
const selectedId = ref('')
const shownMode = ref<Mode>('taxi')
const loading = ref(false)
const error = ref('')
const menuOpen = ref(false)
const copied = ref(false)
const copiedCard = ref('')
const locating = ref(false)
const elapsed = ref(0)
let progressTimer: ReturnType<typeof setInterval> | null = null
const progressStages = [
  { t: 0, percent: 18, text: '1/4 规划司机主干路线' },
  { t: 3, percent: 42, text: '2/4 搜索沿途候选点' },
  { t: 8, percent: 78, text: '3/4 核验路网与接驳方案' },
]
const progressStage = computed(() => [...progressStages].reverse().find(s => elapsed.value >= s.t)!)
function stopProgress() {
  if (progressTimer !== null) clearInterval(progressTimer)
  progressTimer = null
}
function startProgress() {
  stopProgress()
  elapsed.value = 0
  const started = Date.now()
  progressTimer = setInterval(() => { elapsed.value = (Date.now() - started) / 1000 }, 300)
}
function useCurrentLocation() {
  if (locating.value) return
  if (!navigator.geolocation) { error.value = '当前浏览器不支持定位，请手动选择起点。'; return }
  locating.value = true
  navigator.geolocation.getCurrentPosition(position => {
    origin.value = { name: '当前位置', point: wgs84ToGcj02({ lon: position.coords.longitude, lat: position.coords.latitude }) }
    locating.value = false
    error.value = ''
  }, () => {
    locating.value = false
    error.value = '无法获取当前位置，请检查定位权限，或手动选择起点。'
  }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 })
}
function cardKey(r: Recommendation) {
  return `${r.id}:${r.suggested_mode}`
}
function cardMode(r: Recommendation) {
  const option = r.options.find(o => o.mode === r.suggested_mode)
  return option?.direct_walk ? '直接步行' : r.suggested_mode === 'taxi' ? '打车' : '公交 / 地铁'
}
function pointDistance(a: Place, b: Place) {
  const rad = Math.PI / 180
  const dLat = (a.point.lat - b.point.lat) * rad
  const dLon = (a.point.lon - b.point.lon) * rad
  return 12742000 * Math.asin(Math.min(1, Math.sqrt(Math.sin(dLat / 2) ** 2
    + Math.cos(a.point.lat * rad) * Math.cos(b.point.lat * rad) * Math.sin(dLon / 2) ** 2)))
}
function nearbyCardNote(r: Recommendation) {
  const others = (result.value?.recommendations || []).filter(other => cardKey(other) !== cardKey(r))
  const same = others.find(other => other.point.point.lon === r.point.point.lon && other.point.point.lat === r.point.point.lat)
  const nearby = others.filter(other => other.point.name === r.point.name && pointDistance(r.point, other.point) > 0
    && pointDistance(r.point, other.point) <= 300).sort((a, b) => pointDistance(r.point, a.point) - pointDistance(r.point, b.point))[0]
  return [same ? `同一下车点另有${cardMode(same)}方案` : '',
    nearby ? `另有同名下车点，相距约${Math.max(1, Math.round(pointDistance(r.point, nearby.point)))}米（直线）` : ''].filter(Boolean).join('；')
}
function markerUrl(r: Recommendation) {
  return `https://uri.amap.com/marker?position=${r.point.point.lon},${r.point.point.lat}&name=${encodeURIComponent(r.point.name)}&callnative=1`
}
function navigationUrl(r: Recommendation) {
  const req = snapshot.value
  if (!req) return markerUrl(r)
  const place = (p: Place) => `${p.point.lon},${p.point.lat},${encodeURIComponent(p.name)}`
  // https://developer.amap.com/api/uri-api/guide/travel/route
  return `https://uri.amap.com/navigation?from=${place(req.origin)}&to=${place(req.driver_destination)}&via=${place(r.point)}&mode=car&policy=0&callnative=1`
}
async function copyNegotiation(r: Recommendation) {
  if (result.value?.mode !== 'live' || stale.value || expired.value) return
  const opt = r.options.find(o => o.mode === r.suggested_mode)!
  const onward = opt.direct_walk ? '直接步行回家' : opt.mode === 'taxi' ? '打车' : '乘地铁/公交'
  const text = `【顺路下车推荐】建议在「${r.point.name}」下车，司机增加约${duration(r.driver_detour_s)}（含停靠）；我下车后${onward}，乘客全程约${duration(opt.total_duration_s ?? opt.duration_s)}，后续费用${fare(opt.fare_yuan)}。停靠条件待核实。\n司机高德导航（A→P→B）：${navigationUrl(r)}`
  try {
    await navigator.clipboard.writeText(text)
    copiedCard.value = cardKey(r)
    setTimeout(() => { copiedCard.value = '' }, 2200)
  } catch { window.prompt('请复制以下协商文案：', text) }
}
const snapshot = ref<RecommendationRequest | null>(null)
const stale = ref(false)
const expired = ref(false)
let generation = 0
let controller: AbortController | null = null
const ageTimer = setInterval(() => {
  expired.value = !!result.value?.expires_at && Date.now() >= Date.parse(result.value.expires_at)
}, 1000)
onBeforeUnmount(() => { clearInterval(ageTimer); stopProgress(); controller?.abort() })
function cancel() {
  generation++
  controller?.abort()
  controller = null
  loading.value = false
  stopProgress()
}
watch([origin, driverDestination, passengerDestination, detour, taxiFareLimit, selectedModes, priority, walkLimit, transfers, departure], () => {
  stale.value = true
  cancel()
}, { deep: true, flush: 'sync' })
watch(manualMode, cancel)
const active = computed<Recommendation | null>(() => result.value?.recommendations.find(x => cardKey(x) === selectedId.value) || result.value?.recommendations[0] || null)
const activeOption = computed(() => active.value?.options.find(x => x.mode === shownMode.value) || null)
const canSubmit = computed(() => !!origin.value && !!driverDestination.value && !!passengerDestination.value && selectedModes.value.length > 0)
const currentPreferences = computed(() => ({ max_detour_minutes: detour.value,
  max_taxi_fare_yuan: taxiFareLimit.value, modes: selectedModes.value,
  priority: priority.value, max_walking_m: walkLimit.value, max_transfers: transfers.value }))

function toggleMode(next: Mode) {
  if (selectedModes.value.includes(next)) {
    if (selectedModes.value.length > 1) selectedModes.value = selectedModes.value.filter(x => x !== next)
  } else {
    selectedModes.value = [...selectedModes.value, next]
  }
}
function pick(r: Recommendation) {
  selectedId.value = cardKey(r)
  shownMode.value = r.suggested_mode
}
async function run() {
  if (!canSubmit.value || loading.value) return
  const requestId = ++generation
  controller = new AbortController()
  loading.value = true
  error.value = ''
  startProgress()
  try {
    const body: RecommendationRequest = {
      origin: origin.value!, driver_destination: driverDestination.value!,
      passenger_destination: passengerDestination.value!,
      preferences: {
        max_detour_minutes: detour.value, modes: selectedModes.value,
        max_taxi_fare_yuan: taxiFareLimit.value,
        priority: priority.value, max_walking_m: walkLimit.value,
        max_transfers: transfers.value,
      },
      departure_time: departure.value ? new Date(`${departure.value}:00+08:00`).toISOString() : null,
    }
    const frozen = JSON.parse(JSON.stringify(body)) as RecommendationRequest
    const response = await apiPost<RecommendationResponse>('/api/recommend', frozen, controller.signal)
    if (requestId !== generation) return
    result.value = response
    snapshot.value = frozen
    stale.value = false
    expired.value = false
    mode.value = result.value.mode
    selectedId.value = result.value.recommendations[0] ? cardKey(result.value.recommendations[0]) : ''
    shownMode.value = result.value.recommendations[0]?.suggested_mode || 'taxi'
  } catch (e) {
    if (requestId !== generation) return
    result.value = null
    error.value = e instanceof Error ? e.message : '计算失败，请稍后重试'
  } finally {
    if (requestId === generation) { loading.value = false; stopProgress() }
  }
}
function viewPlace() {
  if (!active.value || result.value?.mode !== 'live' || stale.value || expired.value) return
  const { lon, lat } = active.value.point.point
  const title = encodeURIComponent(active.value.point.name)
  window.open(`https://uri.amap.com/marker?position=${lon},${lat}&name=${title}`, '_blank', 'noopener,noreferrer')
}
async function copyPlace() {
  if (!active.value || result.value?.mode !== 'live' || stale.value || expired.value) return
  const { lon, lat } = active.value.point.point
  const text = `${active.value.point.name}（停靠合法性待核实）\n坐标(GCJ-02)：${lon},${lat}\n仅供确认地点，不代表可停车。\n司机起点：${snapshot.value?.origin.name}\n司机途经下车点后前往：${snapshot.value?.driver_destination.name}（${snapshot.value?.driver_destination.point.lon},${snapshot.value?.driver_destination.point.lat}）\n乘客接驳：${activeOption.value?.boarding_stop?.name ?? "打车接车位置待确认"}（${activeOption.value?.boarding_stop?.point.lon ?? "待确认"},${activeOption.value?.boarding_stop?.point.lat ?? "待确认"}）\n${activeOption.value?.details ?? ""}`
  try {
    const driver = active.value.driver_route
    const driverSummary = `\n司机经停路线：${snapshot.value?.origin.name} → ${active.value.highway_exit ? `${active.value.highway_exit.name} → ` : ''}${active.value.point.name} → ${snapshot.value?.driver_destination.name}\n司机总里程：${distance(driver.distance_m)}；总耗时（含停靠）：${duration(driver.duration_s + active.value.stop_duration_s)}；增加：${duration(active.value.driver_detour_s)}\n行驶路段：\n${driver.driving_steps?.map((step, index) => `${index + 1}. ${step.road_name || '未命名道路'} ${step.instruction || step.action || ''}`).join('\n') || '未提供，请重新计算'}`
    await navigator.clipboard.writeText(text + driverSummary)
    copied.value = true
    setTimeout(() => { copied.value = false }, 2200)
  } catch {
    error.value = '复制失败，请检查浏览器剪贴板权限。'
  }
}
onMounted(async () => {
  try {
    const config = await apiGet<{ mode: 'demo' | 'live'; map_proxy_ready: boolean }>('/api/config')
    mode.value = config.mode
    proxyReady.value = config.map_proxy_ready
    if (config.mode === 'demo' && !manualMode.value) await run()
  } catch {
    error.value = '无法连接后端。请先启动 FastAPI 服务，再刷新页面。'
  }
})
</script>

<template>
  <SmartPlanner v-if="!isDebug" :result="result" :active="active" :snapshot="snapshot"
    :origin="origin" :driver-destination="driverDestination" :passenger-destination="passengerDestination"
    :shown-mode="shownMode" :proxy-ready="proxyReady" :mode="mode" :loading="loading" :elapsed="elapsed"
    :error="error" :stale="stale" :expired="expired" :can-submit="canSubmit"
    @run="run" @cancel="cancel" @pick="pick">
    <template #navigation><a href="/debug" @click="navigatePage($event, '/debug')">开发调试</a></template>
    <template #trip>
      <PlaceField v-model="origin" kind="origin" label="共同出发地" placeholder="搜索司机与乘客的出发位置" />
      <PlaceField v-model="driverDestination" kind="driver" label="司机目的地" placeholder="搜索司机目的地" />
      <PlaceField v-model="passengerDestination" kind="passenger" label="乘客目的地" placeholder="搜索您的目的地" />
      <button class="current-location" :disabled="locating" @click="useCurrentLocation"><MapPin :size="14" />{{ locating ? '正在定位…' : '使用当前位置' }}</button>
    </template>
    <template #preferences>
        <div class="setting-row"><label for="detour">司机最多增加总时间（含停靠）</label><strong class="detour-value">{{ detour }} <small>分钟</small></strong></div>
        <input id="detour" v-model.number="detour" class="detour-slider" type="range" min="0" max="120" step="1" :style="{ '--progress': `${detour / 120 * 100}%` }" />
        <div class="range-ends"><span>0 分钟 · 不接受额外耗时</span><span>120 分钟 · 2 小时</span></div>

        <label v-if="!manualMode" class="form-label">朋友下车后的交通方式</label>
        <div v-if="!manualMode" class="transport-toggles">
          <button :class="{ checked: selectedModes.includes('taxi') }" @click="toggleMode('taxi')"><CarFront :size="19" />打车 <Check v-if="selectedModes.includes('taxi')" :size="15" /></button>
          <button :class="{ checked: selectedModes.includes('transit') }" @click="toggleMode('transit')"><BusFront :size="19" />公交 / 地铁 <Check v-if="selectedModes.includes('transit')" :size="15" /></button>
        </div>
        <div v-if="manualMode || selectedModes.includes('taxi')" class="taxi-budget-control">
          <div class="setting-row"><label for="taxi-fare-limit">乘客打车费用上限</label><strong class="detour-value">{{ taxiFareLimit }} <small>元</small></strong></div>
          <input id="taxi-fare-limit" v-model.number="taxiFareLimit" class="detour-slider" type="range" min="0" max="200" step="1" :style="{ '--progress': `${taxiFareLimit / 200 * 100}%` }" />
          <div class="range-ends"><span>0 元</span><span>200 元</span></div>
          <p v-if="isDebug" class="transit-rule">{{ manualMode ? '测试模式会展示超出预算的测算结果，并标明是否在预算内。' : '仅推荐预估打车费不超过此金额的方案；费用未知时不推荐打车。' }}</p>
        </div>
        <p v-if="isDebug && !manualMode && selectedModes.includes('transit')" class="transit-rule"><BusFront :size="14" /> 公交 / 地铁优先到站步行≤300米，最多480米；超出300米增加评分惩罚，打车不受此限制。</p>
        <label v-if="!manualMode" class="form-label">推荐偏好</label>
        <div v-if="!manualMode" class="priority-tabs">
          <button :class="{ active: priority === 'balanced' }" @click="priority = 'balanced'">均衡推荐</button>
          <button :class="{ active: priority === 'time' }" @click="priority = 'time'">省时间</button>
          <button :class="{ active: priority === 'cost' }" @click="priority = 'cost'">省费用</button>
        </div>
        <p v-if="isDebug && !manualMode && selectedModes.includes('taxi')" class="transit-rule">打车按所选偏好综合比较司机增加时间、乘客全程时间和车费：省时间更重视耗时，省费用更重视车费，均衡兼顾两者。费用上限与司机绕路上限仍生效。</p>
        <button v-if="!isDebug" class="advanced-toggle" :aria-expanded="advancedOpen" @click="advancedOpen = !advancedOpen">{{ advancedOpen ? '收起更多设置' : '更多设置 · 步行、换乘、出发时间' }}</button>
        <div v-show="isDebug || advancedOpen">
        <div v-if="!manualMode && selectedModes.includes('transit')" class="extra-grid">
          <label>公交全程步行上限<select v-model.number="walkLimit"><option :value="500">500 米</option><option :value="1000">1000 米</option><option :value="1200">1200 米</option><option :value="2000">2000 米</option><option :value="3000">3000 米</option></select></label>
          <label>最大换乘次数<select v-model.number="transfers"><option :value="0">不换乘</option><option :value="1">1 次</option><option :value="2">2 次</option><option :value="3">3 次</option><option :value="4">4 次</option></select></label>
        </div>
        <label class="departure-label">出发时间（中国时间，可选）<input v-model="departure" type="datetime-local" /></label>
        </div>

    </template>
    <template #details>
          <button v-if="active && !isDebug" class="advanced-toggle route-toggle" :aria-expanded="routeDetailsOpen" @click="routeDetailsOpen = !routeDetailsOpen">{{ routeDetailsOpen ? '收起司机路线详情' : '查看司机路线详情' }}</button>
          <DriverItinerary v-if="active && result && (isDebug || routeDetailsOpen)" :key="cardKey(active)" :origin="snapshot?.origin ?? null"
            :destination="snapshot?.driver_destination ?? null" :baseline="result.baseline_route"
            :recommendation="active" :demo="result.mode === 'demo'" />

          <div v-if="active" class="detail-panel">
            <div class="detail-title"><div><span class="eyebrow">SELECTED STOP</span><h3>{{ active.point.name }} · {{ cardMode(active) }}</h3></div><span class="unverified-badge"><CircleAlert :size="14"/>未验证停靠合法性</span></div>
            <p v-if="isDebug" class="dropoff-location">{{ active.dropoff_road ? `所在道路：${active.dropoff_road} · ` : '' }}GCJ-02 坐标：{{ coordinates(active.point.point) }}<br />{{ active.candidate_source === 'roadside' ? '这是道路采样点，尚未对应到具体场所或停车入口。' : '候选名称用于辨认周边位置，实际下车位置以地图 P 标记为准。' }}</p>
            <p v-if="active.highway_exit" class="journey-summary">{{ active.original_exit ? '司机原路线驶出区域' : '提前驶出高速' }}：{{ active.highway_exit.name }} · 已计入司机后续行程耗时</p>
            <p class="reason">{{ active.reason }}</p><p v-if="active.comparison" class="reason">{{ active.comparison }}</p>
            <p v-if="isDebug" class="journey-summary">额外驾驶 {{ duration(active.driving_extra_s ?? Math.max(0, active.driver_detour_s - active.stop_duration_s)) }} · 停靠 {{ duration(active.stop_duration_s) }}</p>
            <p v-if="isDebug" class="journey-summary">共同乘车 {{ duration(active.shared_ride_s) }} ＋ 停靠 {{ active.stop_duration_s / 60 }} 分钟 ＋ 下车后行程</p>
            <div class="detail-modes">
              <button v-for="o in active.options" :key="o.mode" class="detail-mode" :class="{ active: shownMode === o.mode }" @click="shownMode = o.mode">
                <div class="mode-name"><CarFront v-if="o.mode === 'taxi'" :size="18" /><BusFront v-else :size="18" />{{ o.direct_walk ? '¥0 · 直接步行回家' : o.mode === 'taxi' ? '打车方案' : '公交 / 地铁' }}</div>
                <div class="mode-values"><strong>全程 {{ duration(o.total_duration_s ?? o.duration_s) }}</strong><strong>{{ fare(o.fare_yuan) }}</strong></div>
                <span class="mode-description">下车后 {{ duration(o.duration_s) }} · 预计 {{ arrival(o.arrival_at) }} 到达</span>
                <span class="mode-description">{{ o.direct_walk ? `步行 ${o.walking_m} 米，无需接车或换乘` : o.mode === 'taxi' ? '含假设5分钟接车缓冲，实际等待和上车位置待确认' : `到站步行 ${o.boarding_walk_m} 米 · 全程步行 ${o.walking_m} 米 · 换乘 ${o.transfers} 次` }}</span>
              </button>
            </div>
            <div v-if="activeOption?.mode === 'transit' && activeOption.boarding_stop" class="boarding-summary"><BusFront :size="17" /><div><strong>{{ activeOption.boarding_stop.name }}</strong><p>沿步行路线 {{ activeOption.boarding_walk_m }} 米到达{{ activeOption.boarding_kind === 'subway' ? '地铁入口' : '公交站台' }}；{{ (activeOption.boarding_walk_m || 0) > 300 ? '超过300米，已增加步行评分惩罚（上限480米）' : '符合300米优先范围' }}，同等条件优先更靠近站点的下客位置。</p></div></div>
            <details v-if="activeOption?.mode === 'transit' && !activeOption.direct_walk" class="onward-details" :open="isDebug"><summary>查看公交 / 地铁乘坐步骤</summary><TransitItinerary :option="activeOption" /></details>
            <div v-if="activeOption" class="option-note"><Info :size="15" /><span>{{ activeOption.details }}。{{ activeOption.notes.join('；') }}。</span></div>
            <div class="detail-actions">
              <button class="secondary-action" :disabled="result?.mode !== 'live' || stale || expired" @click="viewPlace"><ExternalLink :size="16"/>在高德查看候选位置</button>
              <button class="secondary-action" :disabled="result?.mode !== 'live' || stale || expired" @click="copyPlace"><Check v-if="copied" :size="16"/><MapPin v-else :size="16"/>{{ copied ? '已复制' : '复制候选位置' }}</button>
            </div>
          </div>
          <details v-if="result?.comparisons?.length" class="detail-panel" :open="isDebug"><summary>其他出行方式对照</summary>
            <div v-for="comparison in result.comparisons" :key="comparison.label"><strong>{{ comparison.label }}</strong>
              <div v-for="(o, index) in comparison.options" :key="index"><p>{{ o.mode === 'taxi' ? '打车' : '公交 / 地铁' }} · 全程 {{ duration(o.total_duration_s ?? o.duration_s) }} · {{ fare(o.fare_yuan) }}<span v-if="o.walking_m != null"> · 步行{{ o.walking_m }}米</span></p><details v-if="o.mode === 'transit'"><summary>查看公共交通乘坐方式</summary><TransitItinerary :option="o" /></details></div>
            </div></details>

    </template>
  </SmartPlanner>
  <div v-else class="page" :class="isDebug ? 'debug-page' : 'user-page'">
    <header class="topbar">
      <div class="brand">
        <div class="brand-mark"><Route :size="24" :stroke-width="2.7" /></div>
        <div><strong>顺路下车</strong><span>ALONGROUTE</span></div>
      </div>
      <nav class="page-nav" aria-label="页面切换">
        <a href="/user" @click="navigatePage($event, '/user')" :class="{ active: !isDebug }" :aria-current="!isDebug ? 'page' : undefined">出行规划</a>
        <a href="/debug" @click="navigatePage($event, '/debug')" :class="{ active: isDebug }" :aria-current="isDebug ? 'page' : undefined">开发调试</a>
      </nav>
      <div class="header-right">
        <span class="mode-chip" :class="mode === 'live' ? 'real' : 'demo'"><i></i>{{ mode === 'live' ? '真实路线模式' : '体验演示模式' }}</span>
        <button class="header-menu" aria-label="展开菜单" @click="menuOpen = !menuOpen"><Menu :size="22" /></button>
      </div>
    </header>

    <main class="workspace">
      <aside class="sidebar" :class="{ 'mobile-open': menuOpen }">
        <div class="sidebar-heading"><span class="eyebrow">{{ isDebug ? 'DEVELOPER / 调试工作台' : '一起出发，顺路到家' }}</span><button class="close-mobile" @click="menuOpen = false"><X :size="20" /></button></div>
        <h1 v-if="isDebug">路线调试<span>工作台</span></h1>
        <h1 v-else>找到一个<span>刚刚好</span><br />的下车点。</h1>
        <p class="subtitle">{{ isDebug ? '调整参数、核验候选，查看完整请求与响应。' : '你少绕一点路，朋友也能轻松到家。' }}</p>

        <div class="section-head"><span class="step-number">01</span><h2>行程信息</h2><span class="step-caption">填写三处地点</span></div>
        <div class="place-list">
          <PlaceField v-model="origin" kind="origin" label="共同出发地 A" placeholder="搜索出发位置" />
          <button class="current-location" :disabled="locating" @click="useCurrentLocation"><MapPin :size="14" />{{ locating ? '正在定位…' : '用当前定位（车在途中）' }}</button>
          <PlaceField v-model="driverDestination" kind="driver" label="司机目的地 B" placeholder="搜索司机目的地" />
          <PlaceField v-model="passengerDestination" kind="passenger" label="朋友目的地 C" placeholder="搜索朋友目的地" />
        </div>
        <p class="place-hint"><Info :size="14" /> 输入地点后，请选择搜索结果，以获取准确坐标。</p>
        <div v-if="isDebug" class="priority-tabs" aria-label="规划模式">
          <button :class="{ active: !manualMode }" @click="manualMode = false">自动推荐</button>
          <button :class="{ active: manualMode }" @click="manualMode = true">测试模式 · 指定下车点</button>
        </div>

        <div class="section-head control-head"><span class="step-number">02</span><h2>出行偏好</h2><SlidersHorizontal :size="17" class="head-muted" /></div>
        <div class="setting-row"><label for="detour">司机最多增加总时间（含停靠）</label><strong class="detour-value">{{ detour }} <small>分钟</small></strong></div>
        <input id="detour" v-model.number="detour" class="detour-slider" type="range" min="0" max="120" step="1" :style="{ '--progress': `${detour / 120 * 100}%` }" />
        <div class="range-ends"><span>0 分钟 · 不接受额外耗时</span><span>120 分钟 · 2 小时</span></div>

        <label v-if="!manualMode" class="form-label">朋友下车后的交通方式</label>
        <div v-if="!manualMode" class="transport-toggles">
          <button :class="{ checked: selectedModes.includes('taxi') }" @click="toggleMode('taxi')"><CarFront :size="19" />打车 <Check v-if="selectedModes.includes('taxi')" :size="15" /></button>
          <button :class="{ checked: selectedModes.includes('transit') }" @click="toggleMode('transit')"><BusFront :size="19" />公交 / 地铁 <Check v-if="selectedModes.includes('transit')" :size="15" /></button>
        </div>
        <div v-if="manualMode || selectedModes.includes('taxi')" class="taxi-budget-control">
          <div class="setting-row"><label for="taxi-fare-limit">乘客打车费用上限</label><strong class="detour-value">{{ taxiFareLimit }} <small>元</small></strong></div>
          <input id="taxi-fare-limit" v-model.number="taxiFareLimit" class="detour-slider" type="range" min="0" max="200" step="1" :style="{ '--progress': `${taxiFareLimit / 200 * 100}%` }" />
          <div class="range-ends"><span>0 元</span><span>200 元</span></div>
          <p v-if="isDebug" class="transit-rule">{{ manualMode ? '测试模式会展示超出预算的测算结果，并标明是否在预算内。' : '仅推荐预估打车费不超过此金额的方案；费用未知时不推荐打车。' }}</p>
        </div>
        <p v-if="isDebug && !manualMode && selectedModes.includes('transit')" class="transit-rule"><BusFront :size="14" /> 公交 / 地铁优先到站步行≤300米，最多480米；超出300米增加评分惩罚，打车不受此限制。</p>
        <label v-if="!manualMode" class="form-label">推荐偏好</label>
        <div v-if="!manualMode" class="priority-tabs">
          <button :class="{ active: priority === 'balanced' }" @click="priority = 'balanced'">均衡推荐</button>
          <button :class="{ active: priority === 'time' }" @click="priority = 'time'">省时间</button>
          <button :class="{ active: priority === 'cost' }" @click="priority = 'cost'">省费用</button>
        </div>
        <p v-if="isDebug && !manualMode && selectedModes.includes('taxi')" class="transit-rule">打车按所选偏好综合比较司机增加时间、乘客全程时间和车费：省时间更重视耗时，省费用更重视车费，均衡兼顾两者。费用上限与司机绕路上限仍生效。</p>
        <button v-if="!isDebug" class="advanced-toggle" :aria-expanded="advancedOpen" @click="advancedOpen = !advancedOpen">{{ advancedOpen ? '收起更多设置' : '更多设置 · 步行、换乘、出发时间' }}</button>
        <div v-show="isDebug || advancedOpen">
        <div v-if="!manualMode && selectedModes.includes('transit')" class="extra-grid">
          <label>公交全程步行上限<select v-model.number="walkLimit"><option :value="500">500 米</option><option :value="1000">1000 米</option><option :value="1200">1200 米</option><option :value="2000">2000 米</option><option :value="3000">3000 米</option></select></label>
          <label>最大换乘次数<select v-model.number="transfers"><option :value="0">不换乘</option><option :value="1">1 次</option><option :value="2">2 次</option><option :value="3">3 次</option><option :value="4">4 次</option></select></label>
        </div>
        <label class="departure-label">出发时间（中国时间，可选）<input v-model="departure" type="datetime-local" /></label>
        </div>

        <button v-if="!manualMode" class="run-button" :disabled="!canSubmit || loading" @click="run">
          <LoaderCircle v-if="loading" :size="19" class="spinning" />
          <Sparkles v-else :size="19" />{{ loading ? '正在计算路线...' : '智能寻找下车点' }}<ArrowRight v-if="!loading" :size="19" />
        </button>
        <p v-else class="transit-rule">在指定下车点面板填写 P 并点击测算，可查看绕路时间、后续打车费用和乘客全程时间。</p>
        <div class="security-hint"><ShieldAlert :size="16" /> 下车前确认临停条件，避开高速、快速路及禁停路段，不占用公交站台。</div>
      </aside>

      <section class="content">
        <DebugPanel v-if="isDebug" :result="result" :snapshot="snapshot" :active="active"
          :stale="stale" :expired="expired" :loading="loading" :mode="mode" :proxy-ready="proxyReady" @pick="pick" />
        <ManualDropoffPanel v-if="manualMode" :origin="origin" :driver-destination="driverDestination"
          :passenger-destination="passengerDestination" :preferences="currentPreferences"
          :departure="departure" :live="mode === 'live'" />
        <template v-else>
        <div class="map-shell">
          <MapCanvas :result="result" :active="active" :origin="snapshot?.origin ?? origin" :driver-destination="snapshot?.driver_destination ?? driverDestination"
            :passenger-destination="snapshot?.passenger_destination ?? passengerDestination" :shown-mode="shownMode" :proxy-ready="proxyReady" />
          <div class="map-floating-top">
            <span><Compass :size="15" /> {{ result ? (result.mode === 'live' ? '真实路线数据 · 停靠待核实' : '演示线路 · 非真实导航') : '填写行程后查看路线' }}</span>
            <span v-if="result" class="map-distance"><Route :size="15" />{{ (result.baseline_route.distance_m / 1000).toFixed(1) }} km 司机原路线</span>
          </div>
        </div>

        <div class="results-area">
          <div class="results-top">
            <div><span class="eyebrow">SMART SUGGESTIONS / 02</span><h2>为你找到的顺路方案 <span v-if="result" class="results-count">{{ result.recommendations.length }}</span></h2></div>
            <button v-if="result" class="recalc" :disabled="loading" @click="run"><RefreshCcw :size="15" />重新计算</button>
          </div>
          <div v-if="stale && result" class="notice caution-notice">条件已变化，当前展示为上一次计算结果，请重新计算。</div>
          <div v-if="expired && result" class="notice caution-notice">结果已超过5分钟，请重新核验后出行。</div>
          <div v-if="loading" class="notice planning-progress" role="status">
            <div><strong>正在规划 · 已用时 {{ elapsed.toFixed(1) }} 秒</strong><p>{{ progressStage.text }}（阶段进度为估计）</p>
              <div class="planning-track"><div :style="{ width: `${progressStage.percent}%` }"></div></div></div>
            <button @click="cancel">取消计算</button>
          </div>
          <p v-if="isDebug && result" class="journey-summary">计算时间：{{ arrival(result.generated_at) }} · 参考出发：{{ arrival(result.departure_at ?? null) }}</p>
          <p v-if="isDebug && result?.search_id" class="journey-summary">搜索编号：{{ result.search_id }} · 用于查询本地搜索日志</p>
          <div v-if="error" class="notice error-notice"><CircleAlert :size="18" /> {{ error }}</div>
          <div v-if="!result && !error" class="empty-state"><MapPin :size="26"/><strong>填写行程，开始智能规划</strong><p>先比较司机绕路成本，再计算乘客后续交通方案。</p></div>
          <div v-else-if="result && !result.recommendations.length" class="empty-state"><BadgeHelp :size="28"/><strong>暂无符合条件的候选点</strong><p>请先检查搜索是否完成及服务状态，再考虑调整行程；公交 / 地铁的到站步行上限为480米，优先选择300米以内方案。</p></div>

          <CandidateReplay v-if="isDebug" :search-id="result?.search_id" />

          <div v-if="result?.recommendations.length" class="result-grid">
            <article v-for="(r, idx) in result.recommendations" :key="cardKey(r)" class="recommendation-card" tabindex="0" :aria-label="`选择${r.point.name} · ${cardMode(r)}`" @keydown.enter.self="pick(r)" @keydown.space.self.prevent="pick(r)" :class="{ picked: active && cardKey(active) === cardKey(r) }" @click="pick(r)">
              <div class="card-header"><span class="card-index">0{{ idx + 1 }}</span><span class="card-title">{{ r.point.name }} · {{ cardMode(r) }}</span><span v-if="r.recommendation_label || idx === 0" class="best-chip">{{ r.recommendation_label || (snapshot?.preferences.priority === 'cost' ? '费用优选' : snapshot?.preferences.priority === 'time' ? '时间优选' : '综合优选') }}</span></div>
              <p class="card-driver-total"><span>司机总时间（含停靠）</span><strong>{{ duration(r.driver_route.duration_s + r.stop_duration_s) }}</strong></p>
              <div class="card-stats"><div><span>司机增加约</span><strong class="green-stat">+{{ detourDuration(r.driver_detour_s) }}</strong></div><div><span>乘客全程</span><strong>{{ duration(r.options.find(x => x.mode === r.suggested_mode)?.total_duration_s ?? 0) }}</strong></div><div><span>后续费用</span><strong>{{ fare(r.options.find(x => x.mode === r.suggested_mode)?.fare_yuan ?? null) }}</strong></div></div>
              <p v-if="nearbyCardNote(r)" class="card-access">{{ nearbyCardNote(r) }}</p>
              <p v-if="isDebug" class="card-location">P · {{ coordinates(r.point.point) }}</p>
              <p v-if="r.options.find(x => x.mode === r.suggested_mode)?.mode === 'transit' && !r.options.find(x => x.mode === r.suggested_mode)?.direct_walk" class="card-access">到站步行 {{ r.options.find(x => x.mode === 'transit')?.boarding_walk_m }} 米 · 公交 / 地铁</p>
              <div class="card-foot"><span><ShieldAlert :size="13"/>待核实上下客</span><ChevronRight :size="17" /></div>
              <div class="card-actions" @click.stop>
                <template v-if="result.mode === 'live' && !stale && !expired">
                  <a :href="navigationUrl(r)" target="_blank" rel="noopener noreferrer">🚗 司机一键导航（A→P→B）</a>
                  <a :href="markerUrl(r)" target="_blank" rel="noopener noreferrer">📍 查看下车点</a>
                  <button @click="copyNegotiation(r)">{{ copiedCard === cardKey(r) ? '✅ 已复制' : '📋 复制微信文案' }}</button>
                </template>
              </div>
            </article>
          </div>

          <button v-if="active && !isDebug" class="advanced-toggle route-toggle" :aria-expanded="routeDetailsOpen" @click="routeDetailsOpen = !routeDetailsOpen">{{ routeDetailsOpen ? '收起司机路线详情' : '查看司机路线详情' }}</button>
          <DriverItinerary v-if="active && result && (isDebug || routeDetailsOpen)" :key="cardKey(active)" :origin="snapshot?.origin ?? null"
            :destination="snapshot?.driver_destination ?? null" :baseline="result.baseline_route"
            :recommendation="active" :demo="result.mode === 'demo'" />

          <div v-if="active" class="detail-panel">
            <div class="detail-title"><div><span class="eyebrow">SELECTED STOP</span><h3>{{ active.point.name }} · {{ cardMode(active) }}</h3></div><span class="unverified-badge"><CircleAlert :size="14"/>未验证停靠合法性</span></div>
            <p v-if="isDebug" class="dropoff-location">{{ active.dropoff_road ? `所在道路：${active.dropoff_road} · ` : '' }}GCJ-02 坐标：{{ coordinates(active.point.point) }}<br />{{ active.candidate_source === 'roadside' ? '这是道路采样点，尚未对应到具体场所或停车入口。' : '候选名称用于辨认周边位置，实际下车位置以地图 P 标记为准。' }}</p>
            <p v-if="active.highway_exit" class="journey-summary">{{ active.original_exit ? '司机原路线驶出区域' : '提前驶出高速' }}：{{ active.highway_exit.name }} · 已计入司机后续行程耗时</p>
            <p class="reason">{{ active.reason }}</p><p v-if="active.comparison" class="reason">{{ active.comparison }}</p>
            <p v-if="isDebug" class="journey-summary">额外驾驶 {{ duration(active.driving_extra_s ?? Math.max(0, active.driver_detour_s - active.stop_duration_s)) }} · 停靠 {{ duration(active.stop_duration_s) }}</p>
            <p v-if="isDebug" class="journey-summary">共同乘车 {{ duration(active.shared_ride_s) }} ＋ 停靠 {{ active.stop_duration_s / 60 }} 分钟 ＋ 下车后行程</p>
            <div class="detail-modes">
              <button v-for="o in active.options" :key="o.mode" class="detail-mode" :class="{ active: shownMode === o.mode }" @click="shownMode = o.mode">
                <div class="mode-name"><CarFront v-if="o.mode === 'taxi'" :size="18" /><BusFront v-else :size="18" />{{ o.direct_walk ? '¥0 · 直接步行回家' : o.mode === 'taxi' ? '打车方案' : '公交 / 地铁' }}</div>
                <div class="mode-values"><strong>全程 {{ duration(o.total_duration_s ?? o.duration_s) }}</strong><strong>{{ fare(o.fare_yuan) }}</strong></div>
                <span class="mode-description">下车后 {{ duration(o.duration_s) }} · 预计 {{ arrival(o.arrival_at) }} 到达</span>
                <span class="mode-description">{{ o.direct_walk ? `步行 ${o.walking_m} 米，无需接车或换乘` : o.mode === 'taxi' ? '含假设5分钟接车缓冲，实际等待和上车位置待确认' : `到站步行 ${o.boarding_walk_m} 米 · 全程步行 ${o.walking_m} 米 · 换乘 ${o.transfers} 次` }}</span>
              </button>
            </div>
            <div v-if="activeOption?.mode === 'transit' && activeOption.boarding_stop" class="boarding-summary"><BusFront :size="17" /><div><strong>{{ activeOption.boarding_stop.name }}</strong><p>沿步行路线 {{ activeOption.boarding_walk_m }} 米到达{{ activeOption.boarding_kind === 'subway' ? '地铁入口' : '公交站台' }}；{{ (activeOption.boarding_walk_m || 0) > 300 ? '超过300米，已增加步行评分惩罚（上限480米）' : '符合300米优先范围' }}，同等条件优先更靠近站点的下客位置。</p></div></div>
            <details v-if="activeOption?.mode === 'transit' && !activeOption.direct_walk" class="onward-details" :open="isDebug"><summary>查看公交 / 地铁乘坐步骤</summary><TransitItinerary :option="activeOption" /></details>
            <div v-if="activeOption" class="option-note"><Info :size="15" /><span>{{ activeOption.details }}。{{ activeOption.notes.join('；') }}。</span></div>
            <div class="detail-actions">
              <button class="secondary-action" :disabled="result?.mode !== 'live' || stale || expired" @click="viewPlace"><ExternalLink :size="16"/>在高德查看候选位置</button>
              <button class="secondary-action" :disabled="result?.mode !== 'live' || stale || expired" @click="copyPlace"><Check v-if="copied" :size="16"/><MapPin v-else :size="16"/>{{ copied ? '已复制' : '复制候选位置' }}</button>
            </div>
          </div>
          <details v-if="result?.comparisons?.length" class="detail-panel" :open="isDebug"><summary>其他出行方式对照</summary>
            <div v-for="comparison in result.comparisons" :key="comparison.label"><strong>{{ comparison.label }}</strong>
              <div v-for="(o, index) in comparison.options" :key="index"><p>{{ o.mode === 'taxi' ? '打车' : '公交 / 地铁' }} · 全程 {{ duration(o.total_duration_s ?? o.duration_s) }} · {{ fare(o.fare_yuan) }}<span v-if="o.walking_m != null"> · 步行{{ o.walking_m }}米</span></p><details v-if="o.mode === 'transit'"><summary>查看公共交通乘坐方式</summary><TransitItinerary :option="o" /></details></div>
            </div></details>
          <div class="footer-note"><Clock3 :size="14" /> 自动推荐仅供辅助决策，路况、公交时刻和停车规则以实际情况为准。</div>
        </div>
        </template>
      </section>
    </main>
    <footer class="footer"><span>© AlongRoute · 为每一段同行找到更好的分岔点</span><span v-if="isDebug"><GitBranch :size="14" /> 本地 MVP v0.1 · <span>{{ mode === 'demo' ? 'Demo' : 'AMap' }}</span></span></footer>
  </div>
</template>
