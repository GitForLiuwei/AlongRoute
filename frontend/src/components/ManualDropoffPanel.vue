<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { CarFront, Clock3, FlaskConical, LoaderCircle, Wallet } from 'lucide-vue-next'
import PlaceField from './PlaceField.vue'
import { apiPost, arrival, coordinates, detourDuration, distance, duration, fare,
  type ManualDropoffResponse, type Place, type Preferences } from '../lib/api'

const props = defineProps<{
  origin: Place | null; driverDestination: Place | null; passengerDestination: Place | null
  preferences: Preferences; departure: string; live: boolean
}>()
const dropoff = ref<Place | null>(null)
const result = ref<ManualDropoffResponse | null>(null)
const loading = ref(false)
const error = ref('')
const stale = ref(false)
const now = ref(Date.now())
let controller: AbortController | null = null
let sequence = 0
const timer = setInterval(() => { now.value = Date.now() }, 1000)
const expired = computed(() => !!result.value && now.value >= Date.parse(result.value.expires_at))
const canSubmit = computed(() => props.live && !!props.origin && !!props.driverDestination
  && !!props.passengerDestination && !!dropoff.value)
function cancel() {
  sequence++
  controller?.abort()
  controller = null
  loading.value = false
}
watch([() => props.origin, () => props.driverDestination, () => props.passengerDestination,
  () => props.preferences, () => props.departure, dropoff], () => {
  stale.value = true
  cancel()
}, { deep: true, flush: 'sync' })
onBeforeUnmount(() => { clearInterval(timer); cancel() })
async function run() {
  if (!canSubmit.value || loading.value) return
  const current = ++sequence
  controller = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const body = JSON.parse(JSON.stringify({
      origin: props.origin, driver_destination: props.driverDestination,
      passenger_destination: props.passengerDestination, dropoff: dropoff.value,
      preferences: props.preferences,
      departure_time: props.departure ? new Date(`${props.departure}:00+08:00`).toISOString() : null,
    }))
    const response = await apiPost<ManualDropoffResponse>('/api/evaluate-dropoff', body, controller.signal)
    if (current !== sequence) return
    result.value = response
    stale.value = false
    now.value = Date.now()
  } catch (e) {
    if (current !== sequence) return
    result.value = null
    error.value = e instanceof Error ? e.message : '测算失败，请重新计算'
  } finally {
    if (current === sequence) loading.value = false
  }
}
</script>

<template>
  <section class="manual-panel">
    <div class="manual-heading"><FlaskConical :size="24" /><div><span class="eyebrow">MANUAL STOP</span><h2>指定下车点测试</h2></div></div>
    <p class="manual-description">填写下车地点 P，测算司机绕路、乘客打车费用及从共同出发地到目的地的全程时间。</p>
    <PlaceField v-model="dropoff" kind="origin" label="指定下车地点 P" placeholder="搜索下车道路、场所或入口" />
    <p class="manual-description">请从搜索结果选择地点。停靠按90秒、接车缓冲按5分钟计算；超过限制仍展示测算值。</p>
    <p v-if="!live" class="notice caution-notice">需要配置真实地图服务后使用指定地点测试。</p>
    <div class="manual-actions"><button class="run-button" :disabled="!canSubmit || loading" @click="run"><LoaderCircle v-if="loading" :size="18" class="spinning" /><FlaskConical v-else :size="18" />{{ loading ? '正在测算指定地点…' : '测算这个下车点' }}</button><button v-if="loading" class="secondary-action" @click="cancel">取消</button></div>
    <p v-if="error" class="notice error-notice">{{ error }}</p>
    <p v-if="stale && result" class="notice caution-notice">输入条件已变化，以下为上次测算，请重新计算。</p>
    <p v-if="expired && result" class="notice caution-notice">结果已超过5分钟，请重新测算；24小时内可能复用地图缓存。</p>
    <template v-if="result">
      <div class="manual-route"><p><strong>司机</strong> {{ result.origin.name }} → <b>{{ result.dropoff.name }}</b> → {{ result.driver_destination.name }}</p><p><strong>乘客</strong> {{ result.origin.name }} → {{ result.dropoff.name }} → {{ result.passenger_destination.name }}</p><small>P 坐标（GCJ-02）：{{ coordinates(result.dropoff.point) }}</small></div>
      <div class="manual-metrics">
        <article><CarFront :size="22" /><span>司机增加时间（含停靠）</span><strong>{{ detourDuration(result.driver_detour_s) }}</strong><small :class="{ exceeded: !result.within_detour_limit }">{{ result.within_detour_limit ? '在限制内' : '超过限制' }} · 上限 {{ result.max_detour_minutes }} 分钟</small></article>
        <article><Wallet :size="22" /><span>乘客后续打车估价</span><strong>{{ fare(result.fare_yuan) }}</strong><small :class="{ exceeded: result.within_taxi_fare_limit === false }">{{ result.within_taxi_fare_limit == null ? '费用未知，无法核验' : result.within_taxi_fare_limit ? '在预算内' : '超过预算' }} · 上限 {{ result.max_taxi_fare_yuan }} 元</small></article>
        <article><Clock3 :size="22" /><span>乘客从 A 到 C 全程</span><strong>{{ result.passenger_total_s == null ? '暂无法确定' : detourDuration(result.passenger_total_s) }}</strong><small>预计到达 {{ arrival(result.arrival_at) }}</small></article>
      </div>
      <div class="manual-breakdown"><h3>时间如何计算</h3><p>司机原路线：{{ duration(result.baseline_route.duration_s) }} · {{ distance(result.baseline_route.distance_m) }}</p><p>司机经 P：驾驶 {{ duration(result.driver_route.duration_s) }} ＋ 停靠 {{ result.stop_duration_s / 60 }} 分钟 · {{ distance(result.driver_route.distance_m) }}</p><p>额外驾驶 {{ detourDuration(result.driving_extra_s) }} ＋ 停靠 {{ result.stop_duration_s / 60 }} 分钟 ＝ 司机增加 {{ detourDuration(result.driver_detour_s) }}</p><p>乘客全程：共同乘车 {{ duration(result.shared_ride_s) }} ＋ 停靠 {{ result.stop_duration_s / 60 }} 分钟 ＋ 接车 {{ result.wait_estimate_s / 60 }} 分钟 ＋ 后续驾车 {{ result.taxi_route ? duration(result.taxi_route.duration_s) : '未知' }}</p><p v-if="result.combined_score != null">综合分 {{ result.combined_score.toFixed(2) }}：司机增加分钟＋乘客全程分钟＋打车费×3，越低越优。</p></div>
      <div class="notice caution-notice"><div><strong>指定地点测算 · 停靠条件待核实</strong><p v-for="warning in result.warnings" :key="warning">{{ warning }}</p></div></div>
      <p class="manual-description">计算于 {{ arrival(result.generated_at) }} · 参考出发 {{ arrival(result.departure_at) }}<br />搜索编号：{{ result.search_id }}</p>
    </template>
  </section>
</template>

<style scoped>
.manual-panel { background: #fff; border: 1px solid #e1e9ed; border-radius: 16px; padding: 24px; }
.manual-heading { display: flex; align-items: center; gap: 12px; color: #148a74; }
.manual-heading h2 { margin: 4px 0 0; color: #263e48; font-size: 23px; }
.manual-description { color: #627587; font-size: 12px; line-height: 1.9; margin: 16px 0; }
.manual-actions { display: flex; gap: 10px; margin-bottom: 20px; }
.manual-actions .run-button { width: auto; padding: 0 24px; }
.manual-route { background: #f2f8f6; border-radius: 12px; padding: 14px 18px; margin: 18px 0; color: #45636a; font-size: 13px; line-height: 1.9; overflow-wrap: anywhere; }
.manual-route p { margin: 4px 0; }.manual-route small { color: #72848a; }
.manual-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 20px 0; }
.manual-metrics article { display: flex; flex-direction: column; align-items: flex-start; gap: 12px; padding: 18px; background: #f8fafb; border: 1px solid #e3ebed; border-radius: 12px; min-width: 0; }
.manual-metrics svg { color: #148a74; }.manual-metrics span { font-size: 12px; color: #627587; }
.manual-metrics strong { font-size: 21px; color: #263e48; overflow-wrap: anywhere; }.manual-metrics small { color: #287764; line-height: 1.8; }.manual-metrics .exceeded { color: #b45b21; }
.manual-breakdown { font-size: 13px; color: #526c78; line-height: 1.9; margin: 22px 0; }.manual-breakdown h3 { color: #263e48; font-size: 16px; }
@media(max-width:1100px) { .manual-metrics { grid-template-columns: 1fr; } }
@media(max-width:640px) { .manual-panel { padding: 18px; }.manual-actions .run-button { flex: 1; padding: 0 12px; } }
</style>
