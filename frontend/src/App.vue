<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, watch, ref } from 'vue'
import { ArrowRight, BadgeHelp, BusFront, CarFront, Check, ChevronRight, CircleAlert, Clock3,
  Compass, ExternalLink, GitBranch, Info, LoaderCircle, MapPin, Menu, RefreshCcw, Route,
  ShieldAlert, SlidersHorizontal, Sparkles, Wallet, X } from 'lucide-vue-next'
import PlaceField from './components/PlaceField.vue'
import MapCanvas from './components/MapCanvas.vue'
import TransitItinerary from './components/TransitItinerary.vue'
import { apiGet, apiPost, duration, fare, arrival, type Place, type Priority, type Mode,
  type Recommendation, type RecommendationResponse, type RecommendationRequest } from './lib/api'

const origin = ref<Place | null>({ name: '乐从中学', point: { lon: 113.105351, lat: 22.940194 }, citycode: '0757', poi_id: 'B02F5076M7' })
const driverDestination = ref<Place | null>({ name: '惠阳区新圩镇东风村卫生站', point: { lon: 114.306364, lat: 22.825135 }, citycode: '0752', poi_id: 'B02F00QI66' })
const passengerDestination = ref<Place | null>({ name: '里城玺樾山', point: { lon: 114.122363, lat: 22.627733 }, citycode: '0755', poi_id: 'B0FFJ3JUUX' })
const detour = ref(5)
const selectedModes = ref<Mode[]>(['taxi', 'transit'])
const priority = ref<Priority>('balanced')
const walkLimit = ref(1200)
const transfers = ref(2)
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
const snapshot = ref<RecommendationRequest | null>(null)
const stale = ref(false)
const expired = ref(false)
let generation = 0
let controller: AbortController | null = null
const ageTimer = setInterval(() => {
  expired.value = !!result.value?.expires_at && Date.now() >= Date.parse(result.value.expires_at)
}, 1000)
onBeforeUnmount(() => { clearInterval(ageTimer); controller?.abort() })
function cancel() {
  generation++
  controller?.abort()
  controller = null
  loading.value = false
}
watch([origin, driverDestination, passengerDestination, detour, selectedModes, priority, walkLimit, transfers, departure], () => {
  stale.value = true
  cancel()
}, { deep: true, flush: 'sync' })
const active = computed<Recommendation | null>(() => result.value?.recommendations.find(x => x.id === selectedId.value) || result.value?.recommendations[0] || null)
const activeOption = computed(() => active.value?.options.find(x => x.mode === shownMode.value) || null)
const canSubmit = computed(() => !!origin.value && !!driverDestination.value && !!passengerDestination.value && selectedModes.value.length > 0)

function toggleMode(next: Mode) {
  if (selectedModes.value.includes(next)) {
    if (selectedModes.value.length > 1) selectedModes.value = selectedModes.value.filter(x => x !== next)
  } else {
    selectedModes.value = [...selectedModes.value, next]
  }
}
function pick(r: Recommendation) {
  selectedId.value = r.id
  shownMode.value = r.suggested_mode
}
async function run() {
  if (!canSubmit.value || loading.value) return
  const requestId = ++generation
  controller = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const body: RecommendationRequest = {
      origin: origin.value!, driver_destination: driverDestination.value!,
      passenger_destination: passengerDestination.value!,
      preferences: {
        max_detour_minutes: detour.value, modes: selectedModes.value,
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
    selectedId.value = result.value.recommendations[0]?.id || ''
    shownMode.value = result.value.recommendations[0]?.suggested_mode || 'taxi'
  } catch (e) {
    if (requestId !== generation) return
    result.value = null
    error.value = e instanceof Error ? e.message : '计算失败，请稍后重试'
  } finally {
    if (requestId === generation) loading.value = false
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
    await navigator.clipboard.writeText(text)
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
    if (config.mode === 'demo') await run()
  } catch {
    error.value = '无法连接后端。请先启动 FastAPI 服务，再刷新页面。'
  }
})
</script>

<template>
  <div class="page">
    <header class="topbar">
      <div class="brand">
        <div class="brand-mark"><Route :size="24" :stroke-width="2.7" /></div>
        <div><strong>顺路下车</strong><span>ALONGROUTE</span></div>
      </div>
      <div class="top-nav"><span class="top-nav-active">智能规划</span><span>使用指南</span><a href="https://developer.amap.com/" target="_blank" rel="noreferrer">高德开放平台 <ExternalLink :size="12" /></a></div>
      <div class="header-right">
        <span class="mode-chip" :class="mode === 'live' ? 'real' : 'demo'"><i></i>{{ mode === 'live' ? '真实路线模式' : '体验演示模式' }}</span>
        <button class="header-menu" aria-label="展开菜单" @click="menuOpen = !menuOpen"><Menu :size="22" /></button>
      </div>
    </header>

    <main class="workspace">
      <aside class="sidebar" :class="{ 'mobile-open': menuOpen }">
        <div class="sidebar-heading"><span class="eyebrow">ROUTE PLANNER / 01</span><button class="close-mobile" @click="menuOpen = false"><X :size="20" /></button></div>
        <h1>找到一个<span>刚刚好</span><br />的下车点。</h1>
        <p class="subtitle">你少绕一点路，朋友也能轻松到家。</p>

        <div class="section-head"><span class="step-number">01</span><h2>行程信息</h2><span class="step-caption">填写三处地点</span></div>
        <div class="place-list">
          <PlaceField v-model="origin" kind="origin" label="共同出发地 A" placeholder="搜索出发位置" />
          <PlaceField v-model="driverDestination" kind="driver" label="司机目的地 B" placeholder="搜索司机目的地" />
          <PlaceField v-model="passengerDestination" kind="passenger" label="朋友目的地 C" placeholder="搜索朋友目的地" />
        </div>
        <p class="place-hint"><Info :size="14" /> 输入地点后，请选择搜索结果，以获取准确坐标。</p>

        <div class="section-head control-head"><span class="step-number">02</span><h2>出行偏好</h2><SlidersHorizontal :size="17" class="head-muted" /></div>
        <div class="setting-row"><label for="detour">司机最多增加总时间（含停靠）</label><strong class="detour-value">{{ detour }} <small>分钟</small></strong></div>
        <input id="detour" v-model.number="detour" class="detour-slider" type="range" min="0" max="30" step="1" :style="{ '--progress': `${detour / 30 * 100}%` }" />
        <div class="range-ends"><span>0 分钟 · 不接受额外耗时</span><span>30 分钟</span></div>

        <label class="form-label">朋友下车后的交通方式</label>
        <div class="transport-toggles">
          <button :class="{ checked: selectedModes.includes('taxi') }" @click="toggleMode('taxi')"><CarFront :size="19" />打车 <Check v-if="selectedModes.includes('taxi')" :size="15" /></button>
          <button :class="{ checked: selectedModes.includes('transit') }" @click="toggleMode('transit')"><BusFront :size="19" />公交 / 地铁 <Check v-if="selectedModes.includes('transit')" :size="15" /></button>
        </div>
        <p v-if="selectedModes.includes('transit')" class="transit-rule"><BusFront :size="14" /> 仅公交 / 地铁方案要求到实际站台或入口步行≤300米，并尽量送近；打车不受此限制。</p>
        <label class="form-label">推荐偏好</label>
        <div class="priority-tabs">
          <button :class="{ active: priority === 'balanced' }" @click="priority = 'balanced'">均衡推荐</button>
          <button :class="{ active: priority === 'time' }" @click="priority = 'time'">省时间</button>
          <button :class="{ active: priority === 'cost' }" @click="priority = 'cost'">省费用</button>
        </div>
        <div class="extra-grid">
          <label>公交全程步行上限<select v-model.number="walkLimit"><option :value="500">500 米</option><option :value="1000">1000 米</option><option :value="1200">1200 米</option><option :value="2000">2000 米</option></select></label>
          <label>最大换乘次数<select v-model.number="transfers"><option :value="0">不换乘</option><option :value="1">1 次</option><option :value="2">2 次</option><option :value="3">3 次</option></select></label>
        </div>
        <label class="departure-label">出发时间（中国时间，可选）<input v-model="departure" type="datetime-local" /></label>

        <button class="run-button" :disabled="!canSubmit || loading" @click="run">
          <LoaderCircle v-if="loading" :size="19" class="spinning" />
          <Sparkles v-else :size="19" />{{ loading ? '正在计算路线...' : '智能寻找下车点' }}<ArrowRight v-if="!loading" :size="19" />
        </button>
        <div class="security-hint"><ShieldAlert :size="16" /> 下车前确认临停条件，避开高速、快速路及禁停路段，不占用公交站台。</div>
      </aside>

      <section class="content">
        <div class="map-shell">
          <MapCanvas :result="result" :active="active" :origin="snapshot?.origin ?? origin" :driver-destination="snapshot?.driver_destination ?? driverDestination"
            :passenger-destination="snapshot?.passenger_destination ?? passengerDestination" :shown-mode="shownMode" :proxy-ready="proxyReady" />
          <div class="map-floating-top">
            <span><Compass :size="15" /> {{ result?.mode === 'live' ? '真实路线数据 · 停靠待核实' : '演示线路 · 非真实导航' }}</span>
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
          <div v-if="loading" class="notice">正在搜索沿途地点并核验路线，可能需要约1分钟。<button @click="cancel">取消计算</button></div>
          <p v-if="result" class="journey-summary">计算时间：{{ arrival(result.generated_at) }} · 参考出发：{{ arrival(result.departure_at ?? null) }}</p>
          <div v-if="error" class="notice error-notice"><CircleAlert :size="18" /> {{ error }}</div>
          <div v-if="result?.warnings?.length" class="notice caution-notice"><ShieldAlert :size="18" /><div><strong>{{ result.mode === 'demo' ? '当前是模拟演示，不可用于真实出行' : '结果仅为待确认候选' }}</strong><p v-for="warning in result.warnings" :key="warning">{{ warning }}</p></div></div>
          <div v-if="!result && !error" class="empty-state"><MapPin :size="26"/><strong>填写行程，开始智能规划</strong><p>先比较司机绕路成本，再计算乘客后续交通方案。</p></div>
          <div v-else-if="result && !result.recommendations.length" class="empty-state"><BadgeHelp :size="28"/><strong>暂无符合条件的候选点</strong><p>请先检查搜索是否完成及服务状态，再考虑调整行程；仅公交 / 地铁方案的到站步行上限为300米。</p></div>

          <div v-if="result?.recommendations.length" class="result-grid">
            <button v-for="(r, idx) in result.recommendations" :key="r.id" class="recommendation-card" :class="{ picked: active?.id === r.id }" @click="pick(r)">
              <div class="card-header"><span class="card-index">0{{ idx + 1 }}</span><span class="card-title">{{ r.point.name }}</span><span v-if="idx === 0" class="best-chip">{{ snapshot?.preferences.priority === 'cost' ? '费用优选' : snapshot?.preferences.priority === 'time' ? '时间优选' : '综合优选' }}</span></div>
              <div class="card-stats"><div><span>司机增加</span><strong class="green-stat">+{{ duration(r.driver_detour_s) }}</strong></div><div><span>乘客全程</span><strong>{{ duration(r.options.find(x => x.mode === r.suggested_mode)?.total_duration_s ?? 0) }}</strong></div><div><span>后续费用</span><strong>{{ fare(r.options.find(x => x.mode === r.suggested_mode)?.fare_yuan ?? null) }}</strong></div></div>
              <p v-if="r.options.find(x => x.mode === r.suggested_mode)?.mode === 'transit'" class="card-access">到站步行 {{ r.options.find(x => x.mode === 'transit')?.boarding_walk_m }} 米 · 公交 / 地铁</p>
              <div class="card-foot"><span><ShieldAlert :size="13"/>待核实上下客</span><ChevronRight :size="17" /></div>
            </button>
          </div>

          <div v-if="active" class="detail-panel">
            <div class="detail-title"><div><span class="eyebrow">SELECTED STOP</span><h3>{{ active.point.name }}</h3></div><span class="unverified-badge"><CircleAlert :size="14"/>未验证停靠合法性</span></div>
            <p v-if="active.highway_exit" class="journey-summary">{{ active.original_exit ? '司机原路线驶出区域' : '提前驶出高速' }}：{{ active.highway_exit.name }} · 已计入司机后续行程耗时</p>
            <p class="reason">{{ active.reason }}</p><p v-if="active.comparison" class="reason">{{ active.comparison }}</p>
            <p class="journey-summary">额外驾驶 {{ duration(active.driving_extra_s ?? Math.max(0, active.driver_detour_s - active.stop_duration_s)) }} · 停靠 {{ duration(active.stop_duration_s) }}</p>
            <p class="journey-summary">共同乘车 {{ duration(active.shared_ride_s) }} ＋ 停靠 {{ active.stop_duration_s / 60 }} 分钟 ＋ 下车后行程</p>
            <div class="detail-modes">
              <button v-for="o in active.options" :key="o.mode" class="detail-mode" :class="{ active: shownMode === o.mode }" @click="shownMode = o.mode">
                <div class="mode-name"><CarFront v-if="o.mode === 'taxi'" :size="18" /><BusFront v-else :size="18" />{{ o.mode === 'taxi' ? '打车方案' : '公交 / 地铁' }}</div>
                <div class="mode-values"><strong>全程 {{ duration(o.total_duration_s ?? o.duration_s) }}</strong><strong>{{ fare(o.fare_yuan) }}</strong></div>
                <span class="mode-description">下车后 {{ duration(o.duration_s) }} · 预计 {{ arrival(o.arrival_at) }} 到达</span>
                <span class="mode-description">{{ o.mode === 'taxi' ? '含假设5分钟接车缓冲，实际等待和上车位置待确认' : `到站步行 ${o.boarding_walk_m} 米 · 全程步行 ${o.walking_m} 米 · 换乘 ${o.transfers} 次` }}</span>
              </button>
            </div>
            <div v-if="activeOption?.mode === 'transit' && activeOption.boarding_stop" class="boarding-summary"><BusFront :size="17" /><div><strong>{{ activeOption.boarding_stop.name }}</strong><p>沿步行路线 {{ activeOption.boarding_walk_m }} 米到达{{ activeOption.boarding_kind === 'subway' ? '地铁入口' : '公交站台' }}，符合300米限制；同等条件优先更靠近站点的下客位置。</p></div></div>
            <TransitItinerary v-if="activeOption?.mode === 'transit'" :option="activeOption" />
            <div v-if="activeOption" class="option-note"><Info :size="15" /><span>{{ activeOption.details }}。{{ activeOption.notes.join('；') }}。</span></div>
            <div class="detail-actions">
              <button class="secondary-action" :disabled="result?.mode !== 'live' || stale || expired" @click="viewPlace"><ExternalLink :size="16"/>在高德查看候选位置</button>
              <button class="secondary-action" :disabled="result?.mode !== 'live' || stale || expired" @click="copyPlace"><Check v-if="copied" :size="16"/><MapPin v-else :size="16"/>{{ copied ? '已复制' : '复制候选位置' }}</button>
            </div>
          </div>
          <div v-if="result?.comparisons?.length" class="detail-panel"><h3>其他出行方式对照</h3>
            <div v-for="comparison in result.comparisons" :key="comparison.label"><strong>{{ comparison.label }}</strong>
              <div v-for="(o, index) in comparison.options" :key="index"><p>{{ o.mode === 'taxi' ? '打车' : '公交 / 地铁' }} · 全程 {{ duration(o.total_duration_s ?? o.duration_s) }} · {{ fare(o.fare_yuan) }}<span v-if="o.walking_m != null"> · 步行{{ o.walking_m }}米</span></p><details v-if="o.mode === 'transit'"><summary>查看公共交通乘坐方式</summary><TransitItinerary :option="o" /></details></div>
            </div></div>
          <div class="footer-note"><Clock3 :size="14" /> 自动推荐仅供辅助决策，路况、公交时刻和停车规则以实际情况为准。</div>
        </div>
      </section>
    </main>
    <footer class="footer"><span>© AlongRoute · 为每一段同行找到更好的分岔点</span><span><GitBranch :size="14" /> 本地 MVP v0.1 · <span>{{ mode === 'demo' ? 'Demo' : 'AMap' }}</span></span></footer>
  </div>
</template>
