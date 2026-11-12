<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ArrowRight, Check, CheckCircle2, ChevronRight, Compass, Copy, Layers3,
  LoaderCircle, MapPin, Navigation, RefreshCcw, Route, Share2, ShieldAlert,
  SlidersHorizontal, Sparkles, TrainFront, CarFront, Eye, EyeOff, X } from 'lucide-vue-next'
import MapCanvas from './MapCanvas.vue'
import { projectionCandidates, type CandidateReplayResponse, type ProjectionCandidate } from '../lib/candidateProjection'
import { apiGet, detourDuration, distance, duration, fare, type Mode, type Place,
  type Recommendation, type RecommendationRequest, type RecommendationResponse } from '../lib/api'

const props = defineProps<{
  result: RecommendationResponse | null; active: Recommendation | null
  snapshot: RecommendationRequest | null; origin: Place | null
  driverDestination: Place | null; passengerDestination: Place | null
  shownMode: Mode; proxyReady: boolean; mode: 'demo' | 'live'
  loading: boolean; elapsed: number; error: string; stale: boolean; expired: boolean; canSubmit: boolean
}>()
const emit = defineEmits<{ run: []; cancel: []; pick: [r: Recommendation] }>()
const preferencesOpen = ref(false)
const detailsOpen = ref(false)
const replayStage = ref(0)
const visibleCandidateCount = ref(Infinity)
const inspectIndex = ref(-1)
const removedKeys = ref<string[]>([])
const showRemovedGhost = ref(false)
let replayTimers: ReturnType<typeof setTimeout>[] = []
function stopReplay() {
  replayTimers.forEach(clearTimeout)
  replayTimers = []
  replayStage.value = 0
  inspectIndex.value = -1
  visibleCandidateCount.value = Infinity
}
function replay() {
  stopReplay()
  projectedView.value = true
  showRemovedGhost.value = false
  removedKeys.value = []
  visibleCandidateCount.value = 0
  replayStage.value = 1
  const candidates = screeningCandidates.value
  const popMs = Math.min(150, Math.max(12, Math.floor(1100 / Math.max(1, candidates.length))))
  const inspectMs = Math.min(560, Math.max(80, Math.floor(6000 / Math.max(1, candidates.length))))
  const inspectionStart = 900 + Math.max(1200, candidates.length * popMs + 150)
  const later = (callback: () => void, ms: number) => replayTimers.push(setTimeout(callback, ms))
  later(() => { replayStage.value = 2 }, 900)
  candidates.forEach((candidate, index) => {
    later(() => { visibleCandidateCount.value = index + 1 }, 900 + index * popMs)
    later(() => { replayStage.value = 3; inspectIndex.value = index }, inspectionStart + index * inspectMs)
    if (candidate.status === 'rejected') later(() => {
      removedKeys.value = [...removedKeys.value, candidate.key]
    }, inspectionStart + index * inspectMs + inspectMs * .65)
  })
  later(() => { replayStage.value = 3 }, inspectionStart)
  const finish = inspectionStart + candidates.length * inspectMs + 350
  later(() => { inspectIndex.value = -1; replayStage.value = 4 }, finish)
  later(stopReplay, finish + 900)
}

const stage = computed(() => props.loading ? (props.elapsed < 3 ? 1 : props.elapsed < 8 ? 2 : 3)
  : props.result && !props.stale && !props.expired ? replayStage.value || 4 : 0)
const stages = ['选主干道', '沿途选点', '路网核验', '推荐锁定']
const status = computed(() => replayStage.value === 3 && inspectingCandidate.value
  ? `第 3 步：回放「${inspectingCandidate.value.name}」核验结果…`
  : replayStage.value === 2 ? `第 2 步：已展示 ${Math.min(visibleCandidateCount.value, screeningCandidates.value.length)} 条候选记录…`
  : props.loading || (replayStage.value > 0 && replayStage.value < 4)
  ? ['第 1 步：正在锁定司机主干路线…', '第 2 步：正在沿途扫描候选停靠点…', '第 3 步：正在核验路网与接驳方案…'][stage.value - 1]
  : props.stale ? '行程已更新，请重新计算'
  : props.expired ? '方案已过期，请重新核验'
  : props.result ? `查询完成：已精选 ${props.result.recommendations.length} 个顺路方案` : '准备好行程，寻找更顺路的下车点')
const usable = computed(() => !!props.active && props.result?.mode === 'live' && !props.stale && !props.expired && !props.loading)
function key(r: Recommendation) { return `${r.id}:${r.suggested_mode}` }
function option(r: Recommendation) { return r.options.find(o => o.mode === r.suggested_mode) }
function modeName(r: Recommendation) { return option(r)?.direct_walk ? '直接步行' : r.suggested_mode === 'taxi' ? '打车接驳' : '公交 / 地铁' }
const shownResult = computed(() => props.loading ? null : props.result)
const shownActive = computed(() => props.loading ? null : props.active)
function navigationUrl(r: Recommendation) {
  const point = (p: Place) => `${p.point.lon},${p.point.lat},${encodeURIComponent(p.name)}`
  return props.snapshot
    ? `https://uri.amap.com/navigation?from=${point(props.snapshot.origin)}&to=${point(props.snapshot.driver_destination)}&via=${point(r.point)}&mode=car&policy=0&callnative=1`
    : `https://uri.amap.com/marker?position=${r.point.point.lon},${r.point.point.lat}&name=${encodeURIComponent(r.point.name)}`
}
const shareText = computed(() => {
  const r = props.active
  if (!r) return ''
  const o = option(r)
  const navigation = navigationUrl(r)
  return `【顺路达 · 顺路下车建议】\n师傅您好！建议在「${r.point.name}」下车。\n🚗 您的行程增加约 ${detourDuration(r.driver_detour_s)}（含停靠），全程约 ${duration(r.driver_route.duration_s + r.stop_duration_s)}。\n${o?.direct_walk ? '🚶 我下车后直接步行' : o?.mode === 'taxi' ? '🚕 我下车后打车' : '🚇 我下车后乘公交 / 地铁'}，接驳约 ${duration(o?.duration_s ?? 0)}，费用 ${fare(o?.fare_yuan ?? null)}。\n📍 位置：${r.point.point.lon}, ${r.point.point.lat}\n停靠条件待核实，咱们确认可以临停后再下车，您看方便吗？\n司机高德导航：${navigation}`
})
const projectedView = ref(true)
const candidateReplay = ref<CandidateReplayResponse | null>(null)
const replayLoading = ref(false)
const diagnosticCandidates = computed(() => projectionCandidates(candidateReplay.value?.events || [], props.result?.recommendations || []))
const screeningCandidates = computed<ProjectionCandidate[]>(() => {
  const candidates = [...diagnosticCandidates.value]
  const ids = new Set(candidates.map(c => c.recommendationId).filter(Boolean))
  for (const r of props.result?.recommendations || []) if (!ids.has(r.id)) {
    ids.add(r.id)
    candidates.push({ key: `final:${r.id}`, name: r.point.name, point: r.point.point,
      status: 'recommended', reason: '最终推荐方案', recommendationId: r.id })
  }
  return candidates
})
const isReplaying = computed(() => replayStage.value > 0 && replayStage.value < 4)
const inspectingCandidate = computed(() => screeningCandidates.value[inspectIndex.value] || null)
const visibleScreening = computed(() => screeningCandidates.value.slice(0, visibleCandidateCount.value))
function screeningState(candidate: ProjectionCandidate, index: number) {
  if (removedKeys.value.includes(candidate.key)) return '已移除'
  if (replayStage.value === 3 && index === inspectIndex.value) return '回放中'
  if (replayStage.value === 3 && index < inspectIndex.value) return candidate.status === 'pending' ? '未核验' : '已核验'
  return '待回放'
}
const rejectedCount = computed(() => diagnosticCandidates.value.filter(c => c.status === 'rejected').length)
const replayNote = computed(() => {
  if (!props.result || props.loading) return ''
  if (replayLoading.value) return '正在读取本次筛选记录…'
  if (!candidateReplay.value?.events.length) return props.result.mode === 'demo'
    ? '演示路线 · 无真实筛选记录' : '未提供筛选记录，仅展示推荐点'
  return `已记录 ${diagnosticCandidates.value.length} 条候选 · 核验未通过 ${rejectedCount.value} 条${candidateReplay.value.finished ? '' : ' · 记录尚未结束'}`
})
const shareDialog = ref<HTMLDialogElement | null>(null)
const shareButton = ref<HTMLButtonElement | null>(null)
const copied = ref(false)
const copyError = ref('')
let copyTimer: ReturnType<typeof setTimeout> | undefined
async function openShare() {
  if (!usable.value) return
  copyError.value = ''; copied.value = false
  await nextTick()
  shareDialog.value?.showModal()
}
function closeShare() { shareDialog.value?.close(); shareButton.value?.focus() }
async function copyShare() {
  if (!usable.value) return
  try {
    await navigator.clipboard.writeText(shareText.value)
    copied.value = true
    copyError.value = ''
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => { copied.value = false }, 2200)
  } catch { copyError.value = '未能自动复制，请选中上方文字手动复制。' }
}
watch(() => [props.loading, props.result, props.stale, props.expired], () => {
  stopReplay()
  if (!usable.value) shareDialog.value?.close()
})
watch(() => [props.result, props.loading, props.stale, props.expired] as const, async (_, __, onCleanup) => {
  candidateReplay.value = null
  replayLoading.value = false
  const result = props.result
  const id = result?.search_id
  if (!result || props.loading || props.stale || props.expired || result.mode !== 'live' || !id || !/^[a-f0-9]{32}$/.test(id)) return
  const controller = new AbortController()
  let current = true
  onCleanup(() => { current = false; controller.abort() })
  replayLoading.value = true
  try {
    const data = await apiGet<CandidateReplayResponse>(`/api/search-replay/${id}`, controller.signal)
    if (!current || data.search_id !== id || !Array.isArray(data.events)) return
    candidateReplay.value = data
    if (data.events.length) { projectedView.value = true; replay() }
  } catch { /* The recommendation remains usable when its optional replay is unavailable. */ }
  finally { if (current) replayLoading.value = false }
}, { immediate: true })
onBeforeUnmount(() => { stopReplay(); clearTimeout(copyTimer) })
</script>

<template>
  <div class="smart-planner bright-planner">
    <header class="smart-header">
      <a class="smart-brand" href="/user" aria-label="顺路达首页">
        <span class="smart-logo"><Compass :size="29" :stroke-width="2.5" /></span>
        <span><strong>顺路达 <b>AlongRoute</b><em>经纬度投影 · 动态筛点</em></strong><small>司机少绕一点路 · 乘客轻松到家</small></span>
      </a>
      <div class="smart-header-end"><span class="smart-mode" :class="{ demo: mode === 'demo' }"><i />{{ mode === 'live' ? '真实路线' : '演示模式' }}</span><slot name="navigation" /></div>
    </header>

    <main class="smart-layout">
      <aside class="smart-sidebar" aria-label="行程与推荐方案">
        <section class="smart-input-section">
          <div class="smart-section-heading"><h1><span>1.</span> 填写双方行程</h1><button class="smart-text-button" :disabled="!result || loading || stale || expired" @click="replay"><RefreshCcw :size="16" />重播地图寻路动画</button></div>
          <div class="smart-places"><slot name="trip" /></div>
          <button class="smart-preferences-toggle" :aria-expanded="preferencesOpen" @click="preferencesOpen = !preferencesOpen"><SlidersHorizontal :size="14" />出行偏好<span>绕路 / 费用 / 交通方式</span><ChevronRight :size="14" :class="{ rotated: preferencesOpen }" /></button>
          <div v-show="preferencesOpen" class="smart-preferences"><slot name="preferences" /></div>
          <button class="smart-search" :disabled="!canSubmit || loading" @click="emit('run')"><LoaderCircle v-if="loading" class="spinning" :size="17" /><Sparkles v-else :size="23" />{{ loading ? '智能寻路与筛点计算中…' : '查询最佳顺路下车方案' }}<ArrowRight v-if="!loading" :size="16" /></button>
        </section>

        <section class="smart-results" aria-label="推荐方案">
          <div class="smart-section-heading"><h2><span>2.</span> {{ isReplaying ? '筛选记录回放' : '推荐顺路下车点' }}</h2><small v-if="result && !loading"><CheckCircle2 :size="16" />{{ result.recommendations.length }} 个方案</small></div>
          <p v-if="stale && result" class="smart-notice">行程或偏好已变化，请重新计算。</p>
          <p v-if="expired && result" class="smart-notice">方案已超过有效期，请重新计算。</p>
          <p v-if="error" class="smart-error" role="alert">{{ error }}</p>
          <div v-if="loading" class="smart-progress" role="status">
            <div class="smart-progress-icon"><Navigation :size="25" class="spinning" /></div>
            <strong>{{ status }}</strong><p>阶段为估计，结果以接口返回为准</p>
            <div class="smart-progress-track"><i :style="{ width: `${stage / 4 * 100}%` }" /></div>
            <div class="smart-progress-bottom"><span>已用时 {{ elapsed.toFixed(1) }} 秒</span><button @click="emit('cancel')">取消计算</button></div>
          </div>
          <div v-else-if="isReplaying && screeningCandidates.length" class="smart-screening">
            <div class="smart-screening-banner"><strong>{{ status }}</strong><p>按本次记录回放；未核验的候选不会标为淘汰。</p><button @click="stopReplay">跳过动画，查看方案</button></div>
            <ol class="smart-screening-list">
              <li v-for="(candidate, index) in visibleScreening" :key="candidate.key" :class="{ inspecting: replayStage === 3 && index === inspectIndex, removed: removedKeys.includes(candidate.key) }">
                <div><strong>{{ candidate.name }}</strong><small>{{ removedKeys.includes(candidate.key) || (replayStage === 3 && index <= inspectIndex) ? candidate.reason : '等待展示本次核验记录' }}</small></div><b>{{ screeningState(candidate, index) }}</b>
              </li>
            </ol>
          </div>
          <div v-else-if="!result" class="smart-empty"><MapPin :size="28" /><strong>下一站，刚刚好。</strong><p>填写行程，让双方都少走一点弯路。</p></div>
          <div v-else-if="!result.recommendations.length" class="smart-empty"><MapPin :size="28" /><strong>暂无符合条件的方案</strong><p>请检查服务状态，或调整行程与偏好后重试。</p></div>
          <div v-else class="smart-card-list">
            <article v-for="(r, index) in result.recommendations" :key="key(r)" class="smart-card recommendation-card" :class="{ picked: active && key(active) === key(r) }" tabindex="0" :aria-label="`选择${r.point.name} · ${modeName(r)}`" @click="emit('pick', r)" @keydown.enter.self="emit('pick', r)" @keydown.space.self.prevent="emit('pick', r)">
              <div class="smart-card-top"><span class="smart-rank">{{ index + 1 }}</span><span class="smart-card-tag">{{ r.recommendation_label || (index === 0 ? (snapshot?.preferences.priority === 'time' ? '时间优选' : snapshot?.preferences.priority === 'cost' ? '费用优选' : '综合优选') : '顺路备选') }}</span><span class="smart-card-mode"><TrainFront v-if="r.suggested_mode === 'transit'" :size="12" /><CarFront v-else :size="12" />{{ modeName(r) }}</span></div>
              <h3>{{ r.point.name }}<ChevronRight :size="16" /></h3>
              <div class="smart-card-stats"><div><span><CarFront :size="14" />司机增加</span><strong>+{{ detourDuration(r.driver_detour_s) }}</strong></div><div><span><Route :size="14" />下车后接驳</span><strong>{{ duration(option(r)?.duration_s ?? 0) }}</strong></div></div>
              <p class="smart-card-meta">乘客全程 {{ duration(option(r)?.total_duration_s ?? option(r)?.duration_s ?? 0) }}<span>后续 {{ fare(option(r)?.fare_yuan ?? null) }}</span></p>
              <p v-if="option(r)?.mode === 'transit' && !option(r)?.direct_walk" class="smart-transit-note"><TrainFront :size="12" />到站步行 {{ option(r)?.boarding_walk_m ?? '待核验' }} 米 · {{ option(r)?.transfers ?? '待核验' }} 次换乘</p>
              <div class="smart-card-foot"><ShieldAlert :size="12" /><span>停靠条件待核实</span><CheckCircle2 v-if="active && key(active) === key(r)" :size="14" /></div>
              <div v-if="active && key(active) === key(r)" class="smart-card-actions" @click.stop>
                <button :disabled="!usable" @click="openShare"><Share2 :size="19" />发给司机确认此点</button>
                <a v-if="usable" :href="navigationUrl(r)" target="_blank" rel="noopener noreferrer"><Navigation :size="19" />导航</a>
                <button v-else disabled class="smart-card-navigation"><Navigation :size="19" />导航</button>
              </div>
            </article>
          </div>
          <p class="smart-sidebar-note"><ShieldAlert :size="13" />{{ result?.mode === 'demo' ? '演示数据仅供体验，不用于实际出行。' : '下车前请与司机确认实际临停条件。' }}</p>
        </section>
      </aside>

      <section class="smart-map-panel" aria-label="路线可视化">
        <MapCanvas theme="bright" :projected="projectedView" :candidates="screeningCandidates" :visible-candidate-count="visibleCandidateCount" :inspect-key="inspectingCandidate?.key" :removed-keys="removedKeys" :show-removed="showRemovedGhost" :search-stage="stage || (shownResult ? 4 : 0)" :result="shownResult" :active="shownActive"
          :origin="shownResult && snapshot ? snapshot.origin : origin" :driver-destination="shownResult && snapshot ? snapshot.driver_destination : driverDestination"
          :passenger-destination="shownResult && snapshot ? snapshot.passenger_destination : passengerDestination"
          :shown-mode="shownMode" :proxy-ready="proxyReady" @pick="emit('pick', $event)" />
        <div class="smart-map-top">
          <div class="smart-map-status" role="status"><span class="smart-status-dot" :class="{ pulse: loading }" /><span>{{ status }}</span><small v-if="replayStage">结果动画回放</small></div>
          <ol class="smart-stages" aria-label="规划阶段"><li v-for="(name, index) in stages" :key="name" :class="{ current: stage === index + 1, complete: stage > index + 1 }"><Check v-if="stage > index + 1" :size="11" /><span v-else>{{ index + 1 }}.</span>{{ name }}<ChevronRight v-if="index < 3" class="stage-arrow" :size="12" /></li></ol>
        </div>
        <div class="smart-map-caption"><div class="smart-map-view" role="group" aria-label="地图显示方式"><button :class="{ active: projectedView }" :aria-pressed="projectedView" @click="projectedView = true">经纬度示意</button><button v-if="proxyReady" :class="{ active: !projectedView }" :aria-pressed="!projectedView" @click="projectedView = false">高德底图</button></div><span v-if="projectedView" class="smart-projection-label">经纬度等比例投影</span><Layers3 :size="13" />{{ result?.mode === 'live' ? '路线来自规划接口 · 停靠条件待核实' : '行程路线示意 · 非真实导航' }}<span v-if="result">{{ distance(result.baseline_route.distance_m) }} 司机原路线</span><button v-if="stage === 4 && rejectedCount" class="smart-ghost-toggle" :aria-pressed="showRemovedGhost" @click="showRemovedGhost = !showRemovedGhost"><EyeOff v-if="showRemovedGhost" :size="14" /><Eye v-else :size="14" />{{ showRemovedGhost ? '隐藏已移除点' : `查看已移除点 (${rejectedCount})` }}</button><small v-if="replayNote" class="smart-replay-note">{{ replayNote }}</small></div>
        <div v-if="active && !loading" class="smart-selected-bar">
          <div class="smart-selected-icon"><MapPin :size="20" /></div><div class="smart-selected-text"><small>当前选择 · {{ modeName(active) }}</small><strong>{{ active.point.name }}</strong></div>
          <button class="smart-detail-button" :aria-expanded="detailsOpen" @click="detailsOpen = !detailsOpen">{{ detailsOpen ? '收起详情' : '路线详情' }}<ChevronRight :size="14" /></button>
          <button ref="shareButton" class="smart-share-button" :disabled="!usable" @click="openShare"><Share2 :size="14" />发送给司机</button>
        </div>
        <div v-if="detailsOpen && active" class="smart-details"><button class="smart-details-close" aria-label="关闭路线详情" @click="detailsOpen = false"><X :size="18" /></button><slot name="details" /></div>
      </section>
    </main>

    <dialog ref="shareDialog" class="smart-share-dialog" aria-labelledby="share-title" @click="($event.target === shareDialog) && closeShare()" @close="shareButton?.focus()">
      <div class="smart-dialog-heading"><h2 id="share-title"><Share2 :size="19" />发送下车建议给司机</h2><button aria-label="关闭分享弹窗" @click="closeShare"><X :size="19" /></button></div>
      <p>把地点和双方行程说清楚，复制后发给司机确认。</p>
      <pre tabindex="0">{{ shareText }}</pre>
      <p v-if="copyError" class="smart-error" role="alert">{{ copyError }}</p>
      <button class="smart-copy-button" @click="copyShare"><Check v-if="copied" :size="16" /><Copy v-else :size="16" />{{ copied ? '已复制，去发给司机吧' : '一键复制协商文案' }}</button>
    </dialog>
  </div>
</template>
