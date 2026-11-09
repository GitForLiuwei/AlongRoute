<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { apiGet, type Point, type Place } from '../lib/api'

const props = defineProps<{ searchId?: string | null }>()
// Diagnostic payloads vary by algorithm version; preserve unknown reason codes explicitly.
type Event = { event: string; sequence: number; elapsed_ms?: number; [key: string]: any }
type Replay = { search_id: string; finished: boolean; events: Event[] }
type Dot = { key: string; point: Point; name: string; status: string; details: string[]; rec?: any }
const opened = ref(false), identifier = ref(''), busy = ref(false), error = ref('')
const data = ref<Replay | null>(null), stage = ref(0), selected = ref(''), query = ref('')
let generation = 0
const stages = ['搜索出口', '生成候选', '名额预选', '路线核验', '最终推荐']
const colors: Record<string, string> = { exit: '#8b5cf6', generated: '#64748b', selected: '#2563eb', quota: '#cbd5e1', pending: '#a16207', rejected: '#dc2626', accepted: '#0891b2', final: '#059669' }
const labels: Record<string, string> = { exit: '搜索到的出口 / Q', generated: '已生成', selected: '进入精算', quota: '名额不足', pending: '尚未完成核验', rejected: '核验未通过', accepted: '核验通过', final: '最终推荐' }
const translations: Record<string, string> = {
  before: '分流前', after: '分流后', to_b: '司机方向', to_c: '乘客方向', near: '近距离', middle: '中距离', far: '远距离',
  split: '司机路线出口', passenger_split: '乘客方向出口', baseline: '原路线普通道路', baseline_surface: '原路线普通道路',
  station_approach: '站点接驳', roadside: '道路采样', parking: '停车场', station_area: '站旁道路',
  missing_continuous_driver_route: '未取得连续司机路线', exit_not_reached_in_order: '未按顺序通过指定出口',
  unusable_or_unknown_road: '道路条件不适用或未知', detour_limit: '司机增加时间超限', no_viable_onward_options: '没有符合要求的后续交通方案',
  evaluation_cancelled_or_budget_exhausted: '核验取消或预算耗尽', map_service_error: '地图服务查询失败',
  early_stop_or_planning_budget: '提前停止或精算预算耗尽', dominated: '存在时间、费用等条件均不差的替代方案',
  fare_limit_or_unknown: '打车费用超限或未知', mode_or_boarding_walking_transfer_limits: '交通方式、到站步行或换乘限制', cost_time_budget: '费用与时间预算',
}
function explain(code: string) { return translations[code] || code }
function pointKey(p: Point) { return `${p.lon.toFixed(6)},${p.lat.toFixed(6)}` }
function valid(p: any): p is Point { return p && Number.isFinite(p.lon) && Number.isFinite(p.lat) }
async function load() {
  const id = identifier.value.trim()
  if (!/^[a-f0-9]{32}$/.test(id)) { error.value = '请输入完整的32位搜索编号'; return }
  const current = ++generation
  busy.value = true; error.value = ''; data.value = null; selected.value = ''
  try {
    const result = await apiGet<Replay>(`/api/search-replay/${id}`)
    if (current === generation) { data.value = result; stage.value = 0 }
  } catch (e) { if (current === generation) error.value = e instanceof Error ? e.message : '回放加载失败' }
  finally { if (current === generation) busy.value = false }
}
watch(() => props.searchId, id => {
  generation++; busy.value = false; data.value = null; error.value = ''; identifier.value = id || ''
  if (opened.value && id) void load()
}, { immediate: true })
function toggle() { opened.value = !opened.value; if (opened.value && identifier.value && !data.value) void load() }
const events = computed(() => data.value?.events || [])
const input = computed(() => events.value.find(e => e.event === 'route_input')?.request)
const result = computed(() => events.value.find(e => e.event === 'route_result'))
const discovery = computed(() => events.value.find(e => e.event === 'discovery_map'))
const summary = computed(() => events.value.find(e => e.event === 'preselection_summary'))
const stats = computed(() => [
  events.value.filter(e => e.event === 'exit_split_discovered').length,
  summary.value?.unique ?? events.value.filter(e => e.event === 'candidate_generated').length,
  summary.value?.selected ?? events.value.filter(e => e.event === 'candidate_started').length,
  events.value.filter(e => e.event === 'candidate_accepted').length,
  result.value?.recommendations?.length ?? 0,
])
const dots = computed(() => {
  const pool = new Map<string, Dot>()
  const byId = new Map<string, Dot[]>()
  const add = (key: string, p: any, name: string, status: string): Dot | undefined => {
    if (!valid(p)) return
    if (!pool.has(key)) pool.set(key, { key, point: p, name, status, details: [] })
    return pool.get(key)
  }
  if (stage.value === 0) {
    for (const e of events.value) {
      if (e.event === 'closest_route_point') add(`q:${e.sequence}`, e.point, 'Q · 司机路线靠近乘客终点的位置', 'exit')
      if (e.event === 'exits_around_q' || e.event === 'destination_exits_selected') {
        for (const exit of e.exits || []) {
          const p = exit.point
          if (valid(p)) { const dot = add(`exit:${pointKey(p)}`, p, exit.name, 'exit'); dot?.details.push(e.event === 'exits_around_q' ? `司机路线 · Q${exit.side === 'before' ? '前' : '后'}` : '乘客方向路线 · 靠近终点的出口') }
        }
      }
    }
    return [...pool.values()]
  }
  for (const e of events.value) {
    const key = e.candidate_key ? JSON.stringify(e.candidate_key) : ''
    if (['candidate_generated', 'candidate_started', 'candidate_accepted', 'candidate_rejected', 'candidate_not_evaluated'].includes(e.event) && key) {
      const coords = String(e.candidate_key[0]).split(',').map(Number)
      const p = e.point?.point || e.recommendation?.point?.point || { lon: coords[0], lat: coords[1] }
      const dot = add(key, p, e.point?.name || e.recommendation?.point?.name || `候选 ${e.candidate_key[0]}`, 'generated')
      if (!dot) continue
      if (e.event === 'candidate_generated') {
        dot.details = [`来源：${[e.discovery_group, e.source, e.direction, e.sample_tier].filter(Boolean).map(explain).join(' · ')}`, `探索路线 ${Number(e.route_index ?? 0) + 1}`, ...(e.candidate_key[2] ? [`出口约束：${e.candidate_key[2]}`] : [])]
      }
      if (stage.value >= 3) {
        if (e.point?.name || e.recommendation?.point?.name) dot.name = e.point?.name || e.recommendation.point.name
        if (e.event === 'candidate_started') dot.status = 'pending'
        if (e.event === 'candidate_not_evaluated') { dot.status = 'pending'; dot.details.push(explain(e.reason)) }
        if (e.event === 'candidate_rejected') { dot.status = 'rejected'; dot.details.push(explain(e.reason)) }
        if (e.event === 'candidate_accepted') {
          dot.status = 'accepted'; dot.rec = e.recommendation
          const id = e.recommendation.id
          byId.set(id, [...(byId.get(id) || []), dot])
        }
      }
    }
    if (stage.value >= 2 && e.event === 'candidate_preselection' && pool.has(key)) {
      const dot = pool.get(key)!
      if (e.decision === 'selected') dot.status = 'selected'
      if (e.decision === 'quota_limit') { dot.status = 'quota'; dot.details.push('未进入本轮精算：候选名额上限') }
    }
  }
  if (stage.value >= 4) {
    for (const e of events.value) if (['taxi_filtered', 'transit_filtered'].includes(e.event)) {
      for (const dot of byId.get(e.candidate_id) || []) dot.details.push(`${e.event === 'taxi_filtered' ? '打车' : '公交'}筛选：${explain(e.reason)}${e.by_candidate_id ? `；替代点 ${e.by_candidate_id}` : ''}`)
    }
    for (const rec of result.value?.recommendations || []) {
      const matched = byId.get(rec.id)
      if (matched?.length) for (const dot of matched) { dot.status = 'final'; dot.details.push(`最终推荐：${rec.reason || rec.label || ''}`) }
      else if (valid(rec.point?.point)) add(`final:${rec.id}`, rec.point.point, rec.point.name, 'final')
    }
  }
  return [...pool.values()]
})
const anchors = computed(() => [
  ['A', input.value?.origin], ['B', input.value?.driver_destination], ['C', input.value?.passenger_destination],
].filter((x): x is [string, Place] => valid(x[1]?.point)))
const bounds = computed(() => {
  const points: Point[] = [...dots.value.map(x => x.point), ...anchors.value.map(x => x[1].point), ...(discovery.value?.driver_routes || []).flat(), ...(discovery.value?.passenger_routes || []).flat()].filter(valid)
  const xs = points.map(p => p.lon), ys = points.map(p => p.lat)
  const minX = Math.min(...xs), minY = Math.min(...ys)
  return { minX, minY, dx: Math.max(.005, Math.max(...xs) - minX), dy: Math.max(.005, Math.max(...ys) - minY) }
})
function xy(p: Point) { const b = bounds.value; return [45 + (p.lon - b.minX) / b.dx * 710, 365 - (p.lat - b.minY) / b.dy * 320] }
function plotted(points: Point[]) { return points.filter(valid).map(p => xy(p).join(',')).join(' ') }
const filtered = computed(() => dots.value.filter(d => `${d.name} ${pointKey(d.point)} ${d.details.join(' ')} ${labels[d.status]}`.toLowerCase().includes(query.value.toLowerCase())))
const active = computed(() => dots.value.find(d => d.key === selected.value))
const legend = computed(() => [...new Set(dots.value.map(d => d.status))])
watch(stage, () => { selected.value = ''; query.value = '' })
</script>

<template>
  <section class="replay-panel">
    <button class="replay-toggle" :aria-expanded="opened" @click="toggle">{{ opened ? '收起' : '展开' }}候选测试流程 · 可视化回放</button>
    <div v-if="opened" class="replay-body">
      <p>先运行自动推荐，再按步骤查看候选如何产生和筛选；也可以粘贴旧搜索编号。回放只读取日志，不产生新的地图规划费用。</p>
      <form class="replay-form" @submit.prevent="load"><input v-model="identifier" aria-label="回放搜索编号" placeholder="输入自动规划的32位搜索编号" maxlength="32" /><button :disabled="busy">{{ busy ? '读取中…' : '加载回放' }}</button></form>
      <p v-if="error" role="alert" class="replay-error">{{ error }}</p>
      <template v-if="data">
        <p>{{ input?.origin?.name }} → 司机：{{ input?.driver_destination?.name }} / 乘客：{{ input?.passenger_destination?.name }}</p>
        <p v-if="!data.finished || !result" class="replay-warning">日志尚未结束或记录不完整；未显示的结果不能视为已淘汰。</p>
        <p v-if="result?.mode === 'demo'" class="replay-warning">这是演示数据，没有真实的出口发现和路线核验过程。</p>
        <p v-else class="replay-warning">{{ result?.search_complete === false ? '本次搜索覆盖不完整。' : '' }}仅展示日志中已记录的点；没有进入候选池的位置不会出现在后续步骤中。{{ discovery ? '细线为探索路线的简化轨迹，浅紫圈为出口查询范围。' : '旧日志不含搜索轨迹，图上不连线。' }}</p>
        <nav class="replay-stages" aria-label="候选筛选步骤"><button v-for="(title, i) in stages" :key="title" :class="{ current: stage === i }" :aria-current="stage === i ? 'step' : undefined" @click="stage = i">{{ i + 1 }}. {{ title }}<small>{{ stats[i] }} {{ i === 0 ? '个分流发现记录' : i === 3 ? '个核验通过记录' : i === 4 ? '个推荐方案' : '个候选' }}</small></button></nav>
        <p>{{ [ 'Q 用于定位司机路线搜索区域；紫点为已选出口和 Q。出口出现不代表收费站位置可直接下客。', '候选来自出口外分流道路、近中远采样、站旁道路及原路线普通道路；不同出口约束保留为不同候选。', '按来源分配有限精算名额，灰点因名额限制未进入精算，不代表路线不可行。', '核验完整司机经停路线、驶出顺序、道路条件、绕路上限和后续交通。通过核验仍不等于确认可停车。', '核验通过后继续比较交通方式、时间和费用。打车综合分＝司机增加分钟＋乘客全程分钟＋车费×3，越低越优。' ][stage] }}</p>
        <div class="replay-layout">
          <div>
            <svg class="replay-map" viewBox="0 0 800 410" role="img" aria-label="候选点空间分布示意图，非道路地图">
              <rect width="800" height="410" fill="#f1f5f9" />
              <path d="M0 100H800 M0 200H800 M0 300H800 M200 0V410 M400 0V410 M600 0V410" stroke="#e2e8f0" />
              <polyline v-for="(line, i) in discovery?.driver_routes || []" :key="`driver:${i}`" :points="plotted(line)" fill="none" stroke="#93b4dd" stroke-width="2" />
              <polyline v-for="(line, i) in discovery?.passenger_routes || []" :key="`passenger:${i}`" :points="plotted(line)" fill="none" stroke="#eab478" stroke-width="2" stroke-dasharray="5 4" />
              <template v-if="stage === 0"><ellipse v-for="(p, i) in discovery?.search_centers || []" :key="`center:${i}`" :cx="xy(p)[0]" :cy="xy(p)[1]" :rx="(discovery?.search_radius_m || 1500) / 102600 / bounds.dx * 710" :ry="(discovery?.search_radius_m || 1500) / 111200 / bounds.dy * 320" fill="#8b5cf6" fill-opacity=".05" stroke="#8b5cf6" stroke-opacity=".2"><title>出口查询中心 · 半径 {{ discovery?.search_radius_m }} 米</title></ellipse></template>
              <g v-for="dot in filtered" :key="dot.key" :transform="`translate(${xy(dot.point).join(',')})`" tabindex="0" role="button" :aria-label="`${dot.name}：${labels[dot.status]}`" @click="selected = dot.key" @keydown.enter.prevent="selected = dot.key" @keydown.space.prevent="selected = dot.key">
                <circle :r="selected === dot.key ? 9 : 5" :fill="colors[dot.status]" stroke="white" stroke-width="1.5" /><title>{{ dot.name }} · {{ labels[dot.status] }}</title>
              </g>
              <g v-for="[label, place] in anchors" :key="label" :transform="`translate(${xy(place.point).join(',')})`"><circle r="12" fill="#0f172a" /><text text-anchor="middle" dy="4" fill="white" font-size="12">{{ label }}</text><title>{{ place.name }}</title></g>
              <text x="18" y="397" fill="#64748b" font-size="12">GCJ-02 坐标示意 · 无道路底图 · 同坐标不同出口约束可能重叠，可从列表逐一查看</text>
            </svg>
            <div v-if="discovery" class="replay-legend"><span>蓝线：司机探索路线</span><span>橙色虚线：乘客方向探索路线</span><span v-if="stage === 0">浅紫圈：出口查询范围</span></div>
            <div class="replay-legend"><span v-for="status in legend" :key="status"><i :style="{ background: colors[status] }" />{{ labels[status] }}</span></div>
          </div>
          <div class="replay-list"><input v-model="query" aria-label="筛选候选点" placeholder="筛选名称、坐标、来源或原因" /><p>{{ filtered.length }} / {{ dots.length }} 条点位记录</p>
            <button v-for="dot in filtered" :key="dot.key" :class="{ picked: selected === dot.key }" @click="selected = dot.key"><i :style="{ background: colors[dot.status] }" /><span>{{ dot.name }}<small>{{ labels[dot.status] }} · {{ pointKey(dot.point) }}</small></span></button>
            <p v-if="!dots.length">此步骤没有记录。旧版算法或演示模式可能未记录此阶段。</p>
          </div>
        </div>
        <article v-if="active" class="replay-detail"><strong>{{ active.name }} · {{ labels[active.status] }}</strong><p v-for="(detail, i) in active.details" :key="i">{{ detail }}</p><template v-if="active.rec"><p>司机增加 {{ (active.rec.driver_detour_s / 60).toFixed(1) }} 分钟</p><p v-for="o in active.rec.options" :key="o.mode">{{ o.mode === 'taxi' ? '打车' : '公交' }} · 乘客全程 {{ ((o.total_duration_s ?? 0) / 60).toFixed(1) }} 分钟 · {{ o.fare_yuan == null ? '费用未知' : `${o.fare_yuan} 元` }}<span v-if="o.mode === 'taxi' && o.fare_yuan != null"> · 综合分 {{ (active.rec.driver_detour_s / 60 + (o.total_duration_s ?? 0) / 60 + o.fare_yuan * 3).toFixed(1) }}</span></p></template></article>
        <details v-if="result?.warnings?.length"><summary>查看本次搜索限制</summary><p v-for="warning in result.warnings" :key="warning">{{ warning }}</p></details>
      </template>
    </div>
  </section>
</template>

<style scoped>
.replay-panel { margin: 16px 0; border: 1px solid #d9e3ee; border-radius: 14px; background: white; }
.replay-toggle { width: 100%; padding: 18px; text-align: left; font-weight: 700; color: #245cb7; background: transparent; border: 0; cursor: pointer; }
.replay-body { padding: 0 18px 18px; font-size: 13px; color: #475569; line-height: 1.7; }
.replay-form { display: flex; gap: 8px; }
input { min-width: 0; border: 1px solid #cbd5e1; border-radius: 7px; padding: 9px; background: white; width: 100%; }
.replay-form input { flex: 1; }
.replay-form button { flex-shrink: 0; border: 0; border-radius: 7px; padding: 8px 12px; background: #2563eb; color: white; cursor: pointer; }
.replay-error { color: #b91c1c; }.replay-warning { background: #fffbeb; padding: 10px; border-radius: 7px; }
.replay-stages { display: flex; flex-wrap: wrap; gap: 6px; margin: 15px 0; }
.replay-stages button { flex: 1; min-width: 100px; border: 1px solid #dbe3ee; border-radius: 8px; padding: 10px; background: #f8fafc; color: #475569; cursor: pointer; }
.replay-stages button.current { background: #eff6ff; border-color: #2563eb; color: #1d4ed8; }
small { display: block; font-size: 11px; }.replay-layout { display: grid; grid-template-columns: minmax(0, 1fr) 260px; gap: 12px; }
.replay-map { width: 100%; border-radius: 10px; }.replay-map g[role=button] { cursor: pointer; }
.replay-map g:focus circle { stroke: #0f172a; stroke-width: 3; }
.replay-list { max-height: 380px; overflow: auto; }.replay-list button { display: flex; gap: 7px; width: 100%; text-align: left; border: 1px solid #e2e8f0; border-radius: 7px; background: white; padding: 9px; margin: 5px 0; color: #334155; cursor: pointer; word-break: break-word; }
.replay-list button.picked { border-color: #2563eb; background: #eff6ff; }
i { display: inline-block; width: 9px; height: 9px; flex-shrink: 0; border-radius: 50%; margin: 6px 5px 0 0; }
.replay-legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 11px; }.replay-detail { background: #f8fafc; border-radius: 8px; padding: 14px; margin: 12px 0; overflow-wrap: anywhere; }.replay-detail p { margin: 5px 0; }
@media(max-width: 1000px) { .replay-layout { grid-template-columns: 1fr; }.replay-list { max-height: 220px; } }
</style>
