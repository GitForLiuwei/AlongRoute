<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { apiGet, coordinates, distance, duration, fare, type Recommendation, type RecommendationRequest, type RecommendationResponse } from '../lib/api'
import { apiTraces, downloadDiagnostic } from '../lib/diagnostics'

const props = defineProps<{
  result: RecommendationResponse | null; snapshot: RecommendationRequest | null; active: Recommendation | null
  stale: boolean; expired: boolean; loading: boolean; mode: string; proxyReady: boolean
}>()
defineEmits<{ pick: [recommendation: Recommendation] }>()
const tab = ref('overview')
const selectedTraceId = ref<number | null>(null)
const onlyErrors = ref(false)
const health = ref('检查中')
const checking = ref(false)
const healthDetail = ref('')
const traces = computed(() => apiTraces.value.filter(t => !onlyErrors.value || t.error || (t.status !== null && t.status >= 400)))
const selectedTrace = computed(() => traces.value.find(t => t.id === selectedTraceId.value) || traces.value[0])
const lastPlanning = computed(() => apiTraces.value.find(t => t.path === '/api/recommend'))
const resultCounts = computed(() => {
  const counts = { taxi: 0, transit: 0, walk: 0 }
  for (const recommendation of props.result?.recommendations ?? []) {
    const option = recommendation.options.find(item => item.mode === recommendation.suggested_mode)
    if (option?.direct_walk) counts.walk++
    else counts[recommendation.suggested_mode]++
  }
  return counts
})
const json = (value: unknown) => JSON.stringify(value, null, 2)
async function checkHealth() {
  if (checking.value) return
  checking.value = true; health.value = '检查中'; healthDetail.value = ''
  try {
    const response = await apiGet<{ status: string; service: string }>('/api/health')
    health.value = response.status === 'ok' ? '正常' : '异常'
    healthDetail.value = json(response)
  } catch (e) {
    health.value = '连接失败'; healthDetail.value = e instanceof Error ? e.message : String(e)
  } finally { checking.value = false }
}
function exportAll() {
  downloadDiagnostic(`alongroute-${props.result?.search_id || Date.now()}.json`, {
    exported_at: new Date().toISOString(), environment: { mode: props.mode, map_proxy_ready: props.proxyReady, health: health.value },
    state: { stale: props.stale, expired: props.expired, loading: props.loading },
    request: props.snapshot, response: props.result, selected_candidate: props.active, requests: apiTraces.value,
  })
}
onMounted(checkHealth)
</script>

<template>
  <section class="debug-console" aria-label="开发诊断面板">
    <div class="debug-heading"><div><span class="eyebrow">DEVELOPER CONSOLE</span><h2>开发诊断</h2><p>本次会话的接口、候选和规划状态</p></div><button @click="exportAll">导出诊断 JSON</button></div>
    <div class="debug-metrics">
      <div><span>API 服务</span><strong :class="{ 'debug-danger': health === '连接失败' }">{{ health }}</strong><button :disabled="checking" :title="healthDetail" @click="checkHealth">重新检查</button></div>
      <div><span>数据 / 地图代理</span><strong>{{ mode === 'live' ? '真实数据' : '演示数据' }}</strong><small>{{ proxyReady ? '代理已配置' : '代理未配置' }} · 地图加载需另行核验</small></div>
      <div><span>自动规划耗时</span><strong>{{ lastPlanning ? (lastPlanning.durationMs === null ? '请求中' : `${(lastPlanning.durationMs / 1000).toFixed(2)} s`) : '尚未请求' }}</strong><small>{{ lastPlanning?.status ? `HTTP ${lastPlanning.status}` : '浏览器端计时' }}</small></div>
      <div><span>已启动核验 / 方案卡片</span><strong>{{ result ? `${result.examined_candidates} / ${result.recommendations.length}` : '— / —' }}</strong><small>{{ result?.search_complete === false ? '搜索覆盖不完整' : result?.search_complete === true ? '本次候选搜索已完成' : '完成状态未提供' }}</small></div>
    </div>
    <div class="debug-state" role="status"><span>{{ loading ? '计算进行中' : '计算空闲' }}</span><span>{{ result ? (stale ? '输入已变更 · 结果待重算' : '输入与结果一致') : '尚无自动规划结果' }}</span><span v-if="result">{{ expired ? '结果已过期' : '结果未过期' }}</span><code v-if="result?.search_id">搜索编号 {{ result.search_id }}</code></div>
    <nav class="debug-tabs" aria-label="诊断分类"><button v-for="item in [['overview', '候选与参数'], ['network', '接口请求'], ['json', '原始数据']]" :key="item[0]" :class="{ active: tab === item[0] }" :aria-pressed="tab === item[0]" @click="tab = item[0]">{{ item[1] }}{{ item[0] === 'network' ? ` (${apiTraces.length})` : '' }}</button><a href="/docs" target="_blank" rel="noopener">API 文档 ↗</a></nav>
    <div v-if="tab === 'overview'" class="debug-body">
      <template v-if="result">
        <section class="debug-planning-notes" aria-label="本次规划说明">
          <h3>本次规划说明</h3>
          <p v-if="result.mode === 'demo'">当前是模拟演示，时间、费用与路线均为虚构数据，不可用于真实出行。</p>
          <p v-else>本次返回 {{ result.recommendations.length }} 张方案卡片：打车 {{ resultCounts.taxi }} 张、公交 / 地铁 {{ resultCounts.transit }} 张、直接步行 {{ resultCounts.walk }} 张。已启动 {{ result.examined_candidates }} 个候选核验；该数量包含被拒绝或未完成的候选，不等于通过数量。</p>
          <p v-if="result.mode === 'live'">{{ result.search_complete === false ? '搜索覆盖不完整，当前仅展示已完成核验的可用方案。' : result.search_complete === true ? '本次候选范围内搜索已完成。' : '接口未提供搜索完成状态。' }}候选搜索受范围、名额和时间预算限制，不保证全局最优。</p>
          <p>打车与公交分别排名，同一下车点的两种方式分别成卡，合计最多六张。混合模式下有足够合格方案时，每种车辆交通方式至少保留两张；直接步行不计入此数量，方案不足时不凑数。</p>
          <p v-if="snapshot?.preferences.modes.includes('transit')">公交到实际首乘站台或入口的步行上限为 {{ result.station_walk_limit_m }} 米；本次全程步行上限 {{ snapshot.preferences.max_walking_m }} 米、换乘上限 {{ snapshot.preferences.max_transfers }} 次。打车不受公交到站距离限制。</p>
          <p v-if="result.mode === 'live'">已取得 {{ result.comparisons?.length ?? 0 }} 组对照方案。{{ result.comparisons?.length ? '具体时间、费用及可用交通方式见下方对照。' : '本次缺少可用对照，不能判断中途下车是否优于直接出行。' }}真实候选的临停合法性仍待现场确认。</p>
          <details v-if="result.warnings.length"><summary>接口诊断与限制（{{ result.warnings.length }} 条）</summary><ul><li v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</li></ul></details>
        </section>
        <dl class="debug-facts"><div><dt>司机基准</dt><dd>{{ duration(result.baseline_route.duration_s) }} / {{ distance(result.baseline_route.distance_m) }} / {{ result.baseline_route.polyline.length }} 个轨迹点</dd></div><div><dt>生成 / 过期（ISO）</dt><dd>{{ result.generated_at }} / {{ result.expires_at || '未提供' }}</dd></div><div><dt>本次参数</dt><dd>绕路 ≤ {{ snapshot?.preferences.max_detour_minutes }} 分钟 · 车费 ≤ {{ snapshot?.preferences.max_taxi_fare_yuan }} 元 · 偏好 {{ snapshot?.preferences.priority }} · 步行 ≤ {{ snapshot?.preferences.max_walking_m }} 米 · 换乘 ≤ {{ snapshot?.preferences.max_transfers }} 次</dd></div></dl>
        <div class="debug-table-wrap"><table class="debug-table"><caption>候选评分和约束 · 点击名称联动下方路线</caption><thead><tr><th>候选 / 来源 / 坐标 GCJ-02</th><th>评分</th><th>额外驾驶 / 停靠</th><th>经停路线</th><th>后续交通</th><th>高速出口 / 停靠状态</th></tr></thead><tbody><tr v-for="r in result.recommendations" :key="`${r.id}:${r.suggested_mode}`" :class="{ selected: active?.id === r.id && active?.suggested_mode === r.suggested_mode }"><td><button @click="$emit('pick', r)">{{ r.point.name }} · {{ r.options[0]?.direct_walk ? '步行' : r.suggested_mode === 'taxi' ? '打车' : '公交 / 地铁' }}</button><small>{{ r.id }} · {{ r.candidate_source }}</small><code>{{ coordinates(r.point.point) }}</code></td><td>{{ r.score.toFixed(3) }}</td><td>{{ (r.driving_extra_s ?? Math.max(0, r.driver_detour_s - r.stop_duration_s)).toFixed(0) }} s / {{ r.stop_duration_s }} s<small>增加合计 {{ r.driver_detour_s }} s</small></td><td>{{ distance(r.driver_route.distance_m) }}<small>{{ r.driver_route.duration_s }} s · {{ r.driver_route.polyline.length }} 点</small></td><td><div v-for="o in r.options" :key="o.mode">{{ o.direct_walk ? '步行' : o.mode }} · {{ fare(o.fare_yuan) }}<small>全程 {{ o.total_duration_s ?? o.duration_s }} s · 到站步行 {{ o.boarding_walk_m ?? '—' }} m · 换乘 {{ o.transfers ?? '—' }}</small></div></td><td>{{ r.highway_exit?.name || '未提供出口' }}<small>{{ r.original_exit ? '原路线驶出' : '非原路线出口 / 未标记' }} · {{ r.stop_status }}</small></td></tr></tbody></table></div>
        <p v-if="!result.recommendations.length">本次没有推荐结果，可在下方候选回放查看筛选记录。</p>
      </template>
      <p v-else>运行一次自动推荐后，可查看本次请求参数、候选评分、路线点数和约束。失败响应、指定下车点测算均可在「接口请求」中查看。</p>
    </div>
    <div v-else-if="tab === 'network'" class="debug-body">
      <div class="debug-tools"><label><input v-model="onlyErrors" type="checkbox" /> 只看失败请求</label><button @click="apiTraces = []">清空记录</button></div>
      <p class="debug-help">保留当前标签页最近 30 次应用 API 请求，刷新后清空。回放接口仅保存摘要；不包含地图 SDK 内部请求。</p>
      <div class="debug-network"><div class="debug-request-list"><button v-for="t in traces" :key="t.id" :class="{ selected: selectedTrace?.id === t.id, 'debug-danger': t.error }" @click="selectedTraceId = t.id"><strong>{{ t.method }} {{ t.path }}</strong><small>{{ new Date(t.startedAt).toLocaleTimeString('zh-CN') }} · {{ t.status ?? '—' }} · {{ t.durationMs === null ? '请求中' : `${t.durationMs} ms` }}{{ t.error ? ' · 失败' : '' }}</small></button><p v-if="!traces.length">没有匹配的请求。</p></div>
        <div v-if="selectedTrace" class="debug-request-detail"><p><b>{{ selectedTrace.method }}</b> {{ selectedTrace.path }}</p><p>搜索编号：{{ selectedTrace.searchId || '未提供' }}<br />服务版本：{{ selectedTrace.release || '未提供' }}<br />日志状态：{{ selectedTrace.logStatus || '未提供' }}</p><p v-if="selectedTrace.error" class="debug-danger" role="alert">{{ selectedTrace.error }}</p><details open><summary>请求体</summary><pre>{{ json(selectedTrace.request) }}</pre></details><details open><summary>响应体</summary><pre>{{ json(selectedTrace.response) }}</pre></details></div>
      </div>
    </div>
    <div v-else class="debug-body"><p class="debug-help">对应最近一次成功的自动规划。失败响应和手动测算请查看「接口请求」。</p><details :open="!!snapshot"><summary>请求快照</summary><pre>{{ json(snapshot) }}</pre></details><details><summary>完整响应（含轨迹）</summary><pre>{{ json(result) }}</pre></details><details><summary>当前候选</summary><pre>{{ json(active) }}</pre></details></div>
    <p class="debug-privacy">导出包含地点与坐标，分享前请检查个人行程信息。</p>
  </section>
</template>

<style scoped>
.debug-planning-notes{padding:14px 16px;margin-bottom:16px;border:1px solid #d6e0ec;border-radius:8px;background:#f8fafc;overflow-wrap:anywhere}.debug-planning-notes h3{margin:0 0 10px;font-size:14px;color:#245ba0}.debug-planning-notes p{margin:8px 0}.debug-planning-notes ul{margin:10px 0;padding-left:20px}.debug-planning-notes li+li{margin-top:6px}
.debug-console{border:1px solid #d6e0ec;border-radius:14px;background:#fff;margin-bottom:20px;overflow:hidden;color:#334155;font-size:12px}.debug-heading{padding:20px;display:flex;justify-content:space-between;gap:12px;align-items:center;background:#f0f5fb}.debug-heading h2{font-size:23px;margin:6px 0}.debug-heading p{margin:0;color:#64748b}.debug-console button,.debug-tabs a{border:1px solid #cbd5e1;border-radius:6px;padding:8px 10px;background:white;color:#245ba0}.debug-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:#e2e8f0;border-block:1px solid #e2e8f0}.debug-metrics>div{background:#fff;display:flex;flex-direction:column;align-items:flex-start;gap:8px;padding:14px}.debug-metrics strong{font-size:18px}.debug-metrics span,.debug-metrics small{color:#64748b}.debug-metrics button{padding:3px 6px;font-size:11px}.debug-state{display:flex;gap:9px;flex-wrap:wrap;padding:12px 18px;font-size:11px;background:#f8fafc;overflow-wrap:anywhere}.debug-state span{background:#e8eff7;padding:4px 6px;border-radius:4px}.debug-tabs{display:flex;gap:8px;padding:12px 18px;border-block:1px solid #e2e8f0;flex-wrap:wrap}.debug-tabs .active{background:#245ba0;color:white;border-color:#245ba0}.debug-tabs a{margin-left:auto}.debug-body{padding:16px;line-height:1.7;min-width:0}.debug-facts{margin:0 0 14px}.debug-facts>div{display:flex;gap:16px;padding:5px 0}.debug-facts dt{min-width:110px;color:#64748b}.debug-facts dd{margin:0;overflow-wrap:anywhere}.debug-table-wrap{overflow:auto}.debug-table{width:100%;border-collapse:collapse;text-align:left;white-space:nowrap}.debug-table caption{text-align:left;color:#64748b;padding-bottom:8px}.debug-table th{background:#f1f5f9;color:#64748b;font-weight:500}.debug-table th,.debug-table td{padding:10px;border-bottom:1px solid #e2e8f0}.debug-table small,.debug-table code{display:block;font-size:11px}.debug-table td:first-child button{text-align:left;max-width:230px;white-space:normal}.selected{background:#edf6ff!important}.debug-tools{display:flex;align-items:center;justify-content:space-between;gap:10px}.debug-help,.debug-privacy{color:#64748b;font-size:11px}.debug-privacy{padding:0 18px 12px;margin:0}.debug-network{display:grid;grid-template-columns:minmax(180px,1fr) minmax(0,2fr);gap:14px}.debug-request-list{max-height:510px;overflow:auto}.debug-request-list button{display:block;width:100%;margin-bottom:6px;text-align:left;overflow-wrap:anywhere}.debug-request-list small{display:block;color:#64748b;margin-top:5px}.debug-request-detail{min-width:0;overflow-wrap:anywhere}.debug-console pre{overflow:auto;max-height:400px;background:#101d30;color:#d2e5fa;border-radius:8px;padding:14px;font-size:11px;line-height:1.7;tab-size:2}.debug-console details{margin:8px 0}.debug-console summary{cursor:pointer;font-weight:600}.debug-danger{color:#b91c1c!important}@media(max-width:640px){.debug-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.debug-heading{padding:14px;align-items:flex-start}.debug-heading>button{flex-shrink:0}.debug-network{grid-template-columns:1fr}.debug-request-list{max-height:200px}.debug-facts>div{display:block}.debug-body{padding:12px}.debug-tabs{padding:10px;gap:5px}.debug-tabs a{margin-left:0}}
</style>
